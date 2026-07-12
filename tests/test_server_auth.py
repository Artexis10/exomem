from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from exomem import server_auth


def test_build_oauth_delegates_token_lifetime_to_fastmcp(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    for key, value in {
        "GITHUB_CLIENT_ID": "client",
        "GITHUB_CLIENT_SECRET": "secret",
        "EXOMEM_GITHUB_USERNAME": "person",
        "EXOMEM_JWT_SIGNING_KEY": "stable-signing-key",
        "EXOMEM_OAUTH_STORAGE_URL": "https://coordinator.example",
        "EXOMEM_OAUTH_STORAGE_NAMESPACE": "vault",
        "EXOMEM_OAUTH_STORAGE_TOKEN": "state-token",
    }.items():
        monkeypatch.setenv(key, value)

    from fastmcp import settings

    monkeypatch.setattr(settings, "home", tmp_path)
    captured: dict[str, Any] = {}

    class RecordingOAuthProxy:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(server_auth, "OAuthProxy", RecordingOAuthProxy)

    result = server_auth.build_oauth(
        require_auth=True,
        base_url="https://memory.example",
    )

    assert isinstance(result, RecordingOAuthProxy)
    assert "fallback_access_token_expiry_seconds" not in captured
    assert captured["jwt_signing_key"] == "stable-signing-key"
    assert isinstance(captured["token_verifier"], server_auth.SingleUserGitHubVerifier)
    assert captured["token_verifier"]._allowed_login == "person"
    assert captured["client_storage"] is not None
