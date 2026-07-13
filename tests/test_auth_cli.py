from __future__ import annotations

import json
import os
from dataclasses import replace

import pytest

from exomem import env_compat, server_auth
from exomem.__main__ import main
from exomem.auth_sessions import SessionRecord, SessionStoreUnavailable


def _record(*, generation: str = "current", status: str = "active") -> SessionRecord:
    return SessionRecord(
        schema_version=1,
        session_id="abcdefghijklmnop",
        token_digest="0" * 64,
        client_id="codex-client",
        scopes=("read",),
        issuer="https://kb.example.com",
        audience="https://kb.example.com/mcp",
        github_user_id=1234,
        github_login="octocat",
        issued_at=1_700_000_000.0,
        generation=generation,
        status=status,  # type: ignore[arg-type]
        revoked_at=1_700_000_001.0 if status == "revoked" else None,
        revocation_reason="operator" if status == "revoked" else None,
    )


class FakeAuthority:
    def __init__(self, records: list[SessionRecord] | None = None) -> None:
        self.records = [_record()] if records is None else records
        self.tombstone_calls: list[tuple[str, str]] = []
        self.replace_calls = 0

    async def list_sessions(self) -> list[SessionRecord]:
        return self.records

    async def current_generation(self) -> str:
        return "current"

    async def tombstone(self, session_id: str, *, reason: str) -> bool:
        self.tombstone_calls.append((session_id, reason))
        return session_id == "abcdefghijklmnop"

    async def replace_generation(self) -> str:
        self.replace_calls += 1
        return "new-secret-generation"


def _install_authority(
    monkeypatch: pytest.MonkeyPatch, authority: FakeAuthority
) -> list[str]:
    dotenv_calls: list[str] = []
    monkeypatch.setenv("EXOMEM_BASE_URL", "https://kb.example.com/")
    monkeypatch.setattr(
        "dotenv.load_dotenv",
        lambda **kwargs: dotenv_calls.append(f"override={kwargs['override']}"),
    )
    monkeypatch.setattr(
        server_auth,
        "build_session_authority",
        lambda *, base_url: (
            authority
            if base_url == "https://kb.example.com"
            else pytest.fail(f"unexpected base URL: {base_url}")
        ),
        raising=False,
    )
    return dotenv_calls


def test_auth_sessions_json_is_secret_free_and_marks_generation_revoked(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    records = [
        _record(),
        replace(_record(), session_id="qrstuvwxyzABCDEF", generation="old"),
    ]
    authority = FakeAuthority(records)
    dotenv_calls = _install_authority(monkeypatch, authority)

    assert main(["auth", "sessions", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert dotenv_calls == ["override=True"]
    assert payload == {
        "sessions": [
            {
                "session_id": "abcdefghijklmnop",
                "client_id": "codex-client",
                "scopes": ["read"],
                "github_login": "octocat",
                "github_user_id": 1234,
                "issued_at": 1_700_000_000.0,
                "status": "active",
            },
            {
                "session_id": "qrstuvwxyzABCDEF",
                "client_id": "codex-client",
                "scopes": ["read"],
                "github_login": "octocat",
                "github_user_id": 1234,
                "issued_at": 1_700_000_000.0,
                "status": "generation_revoked",
            },
        ]
    }
    rendered = json.dumps(payload)
    assert "token_digest" not in rendered
    assert '"generation":' not in rendered
    assert "new-secret-generation" not in rendered


def test_auth_revoke_one_uses_tombstone_and_custom_reason(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    authority = FakeAuthority()
    _install_authority(monkeypatch, authority)

    assert main([
        "auth", "revoke", "abcdefghijklmnop", "--reason", "lost laptop", "--json"
    ]) == 0

    assert authority.tombstone_calls == [("abcdefghijklmnop", "lost laptop")]
    assert json.loads(capsys.readouterr().out) == {
        "revoked": True,
        "session_id": "abcdefghijklmnop",
    }


def test_auth_revoke_all_replaces_generation_without_printing_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    authority = FakeAuthority()
    _install_authority(monkeypatch, authority)

    assert main(["auth", "revoke", "--all", "--json"]) == 0

    assert authority.replace_calls == 1
    output = capsys.readouterr().out
    assert json.loads(output) == {"revoked_all": True}
    assert "new-secret-generation" not in output


@pytest.mark.parametrize(
    "argv",
    [
        ["auth", "revoke"],
        ["auth", "revoke", "abcdefghijklmnop", "--all"],
        ["auth", "revoke", "--all", "--reason", ""],
    ],
)
def test_auth_usage_errors_exit_two(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == 2


def test_auth_missing_config_exits_two(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("dotenv.load_dotenv", lambda **_kwargs: None)
    monkeypatch.delenv("EXOMEM_BASE_URL", raising=False)

    assert main(["auth", "sessions"]) == 2
    assert "EXOMEM_BASE_URL" in capsys.readouterr().err


def test_auth_authority_loads_dotenv_from_service_working_directory(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("EXOMEM_BASE_URL", "https://kb.example.com")
    calls: list[tuple[object, bool]] = []

    def load_env(*, dotenv_path=None, override: bool) -> None:
        calls.append((dotenv_path, override))

    monkeypatch.setattr("dotenv.load_dotenv", load_env)
    monkeypatch.setattr(
        server_auth,
        "build_session_authority",
        lambda *, base_url: FakeAuthority([]),
    )

    assert main(["auth", "sessions", "--json"]) == 0
    assert calls == [(tmp_path / ".env", True)]


def test_auth_promotes_legacy_env_after_loading_dotenv(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    authority = FakeAuthority([])
    monkeypatch.delenv("EXOMEM_BASE_URL", raising=False)
    monkeypatch.delenv("KB_MCP_BASE_URL", raising=False)

    def load_env(*, dotenv_path, override: bool) -> None:
        assert dotenv_path is not None
        assert override is True
        monkeypatch.setenv("KB_MCP_BASE_URL", "https://legacy.example.com")

    seen: list[str] = []
    monkeypatch.setattr("dotenv.load_dotenv", load_env)
    monkeypatch.setattr(env_compat, "_advised", False)
    monkeypatch.setattr(
        server_auth,
        "build_session_authority",
        lambda *, base_url: seen.append(base_url) or authority,
        raising=False,
    )

    assert main(["auth", "sessions", "--json"]) == 0
    assert seen == ["https://legacy.example.com"]
    assert json.loads(capsys.readouterr().out) == {"sessions": []}
    os.environ.pop("EXOMEM_BASE_URL", None)


def test_auth_factory_storage_failure_exits_one_without_details(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("EXOMEM_BASE_URL", "https://kb.example.com")
    monkeypatch.setattr("dotenv.load_dotenv", lambda **_kwargs: None)

    def fail_factory(*, base_url: str):
        raise OSError(f"cannot create store for {base_url} with secret-token")

    monkeypatch.setattr(
        server_auth, "build_session_authority", fail_factory, raising=False
    )

    assert main(["auth", "sessions"]) == 1
    error = capsys.readouterr().err
    assert "session authority unavailable" in error
    assert "secret-token" not in error


def test_auth_real_oauth_configuration_error_exits_two(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The merged session helper uses RuntimeError for invalid OAuth env."""
    monkeypatch.setenv("EXOMEM_BASE_URL", "https://kb.example.com")
    monkeypatch.setenv("GITHUB_CLIENT_ID", "github-client")
    monkeypatch.setenv("GITHUB_CLIENT_SECRET", "github-secret")
    monkeypatch.setenv("EXOMEM_GITHUB_USERNAME", "octocat")
    monkeypatch.setenv("EXOMEM_OAUTH_STORAGE_URL", "https://coordinator.example")
    monkeypatch.delenv("EXOMEM_JWT_SIGNING_KEY", raising=False)
    monkeypatch.setattr("dotenv.load_dotenv", lambda **_kwargs: None)

    def representative_session_helper(*, base_url: str):
        return server_auth.build_oauth(require_auth=True, base_url=base_url)

    monkeypatch.setattr(
        server_auth,
        "build_session_authority",
        representative_session_helper,
        raising=False,
    )

    assert main(["auth", "sessions"]) == 2
    error = capsys.readouterr().err
    assert "auth configuration error" in error
    assert "EXOMEM_JWT_SIGNING_KEY" in error


def test_auth_authority_failure_exits_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    authority = FakeAuthority()

    async def unavailable() -> list[SessionRecord]:
        raise SessionStoreUnavailable("coordinator rejected request with secret-token")

    authority.list_sessions = unavailable  # type: ignore[method-assign]
    _install_authority(monkeypatch, authority)

    assert main(["auth", "sessions"]) == 1
    error = capsys.readouterr().err
    assert "session authority unavailable" in error
    assert "secret-token" not in error
