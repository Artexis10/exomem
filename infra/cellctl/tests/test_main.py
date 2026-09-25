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
