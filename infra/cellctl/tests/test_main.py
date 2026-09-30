"""cellctl's settings load (main.py): a bad chart value fails at startup."""

from __future__ import annotations

import base64
import json

import pytest

from cellctl import main
from cellctl.storage.b2 import B2Config

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
def test_a_model_env_that_redirects_code_or_carries_credentials_fails_at_settings_load(
    monkeypatch, key: str
) -> None:
    _settings_env(monkeypatch, {key: "x"})
    with pytest.raises(ValueError):
        main.build_cluster_config()


@pytest.mark.parametrize(
    "raw", ['{"EXOMEM_EMBED_BACKEND": 1}', '["EXOMEM_EMBED_BACKEND"]', '"onnx"']
)
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


def test_synthetic_platform_bundles_match_cellctl_consumers(monkeypatch) -> None:
    token = {
        "current": "ab" * 32,
        "currentVersion": "2",
        "previous": "cd" * 32,
        "previousVersion": "1",
    }
    backup_key = bytes(range(32))
    backup = {
        "keys": json.dumps({"1": base64.b64encode(backup_key).decode("ascii")}),
        "currentVersion": "1",
    }
    b2 = {"keyId": "synthetic-key-id", "applicationKey": "synthetic-application-key"}
    _secrets_env(monkeypatch, token["current"], token["previous"])
    monkeypatch.setenv("CELLCTL_CELL_TOKEN_KEY_VERSION", token["currentVersion"])
    monkeypatch.setenv("CELLCTL_CELL_TOKEN_KEY_PREVIOUS_VERSION", token["previousVersion"])
    monkeypatch.setenv("CELLCTL_BACKUP_MASTER_KEYS", backup["keys"])
    monkeypatch.setenv("CELLCTL_BACKUP_MASTER_KEY_CURRENT_VERSION", backup["currentVersion"])
    config = main.build_secrets_config()
    assert config.cell_token_key_current == bytes.fromhex(token["current"])
    assert config.cell_token_key_previous == bytes.fromhex(token["previous"])
    assert config.backup_master_keys == {1: backup_key}
    assert config.backup_master_key_current_version == 1
    b2_config = B2Config(
        key_management_key_id=b2["keyId"],
        key_management_application_key=b2["applicationKey"],
        bucket_id="synthetic-bucket",
        account_id="synthetic-account",
    )
    assert b2_config.key_management_key_id == b2["keyId"]
    assert b2_config.key_management_application_key == b2["applicationKey"]


@pytest.mark.parametrize(
    "bad", ["ab" * 31, "ab" * 33, "zz" * 32, "q83vAAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxw="]
)
def test_a_cell_token_key_that_is_not_64_hex_fails_at_settings_load(monkeypatch, bad: str) -> None:
    _secrets_env(monkeypatch, bad)
    with pytest.raises(ValueError):
        main.build_secrets_config()
