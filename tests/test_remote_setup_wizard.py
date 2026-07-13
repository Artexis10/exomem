"""`exomem setup --remote` — the guided remote-connector wizard.

Pure-logic coverage (JWT generation, .env patch/merge, EXOMEM_BASE_URL
validation, OAuth-field rendering) plus orchestration wiring driven entirely
through injected seams (doctor_fn / load_env_fn / env_path / input_fn /
print_fn) — no test writes a real `.env` or touches the network.

NOT covered here (cannot be exercised headlessly — manual-verify): the live
GitHub OAuth flow and the tunnel probes that `doctor --profile remote --probe`
performs. See the module docstring and docs/remote-quickstart.md.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from exomem import remote_setup_wizard as rsw
from exomem.__main__ import main

# ============================================================================
# generate_signing_key
# ============================================================================


def test_generate_signing_key_is_urlsafe_and_long() -> None:
    key = rsw.generate_signing_key()
    # secrets.token_urlsafe(48) -> 64 base64url chars.
    assert len(key) >= 64
    assert set(key) <= set(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    )


def test_generate_signing_key_is_unique_per_call() -> None:
    assert rsw.generate_signing_key() != rsw.generate_signing_key()


# ============================================================================
# validate_base_url
# ============================================================================


@pytest.mark.parametrize(
    "url",
    ["https://kb.example.com", "https://you.ngrok-free.dev", "http://127.0.0.1:8765"],
)
def test_validate_base_url_accepts_clean_origin(url: str) -> None:
    assert rsw.validate_base_url(url) == url


def test_validate_base_url_trims_whitespace() -> None:
    assert rsw.validate_base_url("  https://kb.example.com  ") == "https://kb.example.com"


def test_validate_base_url_rejects_trailing_slash() -> None:
    with pytest.raises(rsw.BaseUrlError, match="trailing slash"):
        rsw.validate_base_url("https://kb.example.com/")


def test_validate_base_url_rejects_mcp_suffix() -> None:
    with pytest.raises(rsw.BaseUrlError, match="/mcp"):
        rsw.validate_base_url("https://kb.example.com/mcp")


def test_validate_base_url_rejects_empty() -> None:
    with pytest.raises(rsw.BaseUrlError, match="empty"):
        rsw.validate_base_url("   ")


def test_validate_base_url_requires_scheme() -> None:
    with pytest.raises(rsw.BaseUrlError, match="scheme"):
        rsw.validate_base_url("kb.example.com")


# ============================================================================
# connector_url / callback_url
# ============================================================================


def test_connector_and_callback_urls() -> None:
    base = "https://kb.example.com"
    assert rsw.connector_url(base) == "https://kb.example.com/mcp"
    assert rsw.callback_url(base) == "https://kb.example.com/auth/callback"


# ============================================================================
# parse_env
# ============================================================================


def test_parse_env_ignores_comments_blanks_and_bad_lines() -> None:
    text = "# comment\n\nEXOMEM_BASE_URL=https://kb.example.com\nBARE_LINE\nA=1\n"
    assert rsw.parse_env(text) == {"EXOMEM_BASE_URL": "https://kb.example.com", "A": "1"}


def test_parse_env_last_write_wins() -> None:
    assert rsw.parse_env("A=1\nA=2\n") == {"A": "2"}


# ============================================================================
# patch_env
# ============================================================================


def test_patch_env_updates_existing_in_place_and_preserves_others() -> None:
    existing = "OTHER=keep\nEXOMEM_VAULT_PATH=/old\nTAIL=z\n"
    out = rsw.patch_env(existing, {"EXOMEM_VAULT_PATH": "/new"})
    lines = out.splitlines()
    # order preserved, only the targeted line changed
    assert lines == ["OTHER=keep", "EXOMEM_VAULT_PATH=/new", "TAIL=z"]


def test_patch_env_appends_new_keys_in_insertion_order() -> None:
    out = rsw.patch_env("A=1\n", {"B": "2", "C": "3"})
    assert out.splitlines() == ["A=1", "B=2", "C=3"]


def test_patch_env_preserves_comments_and_blank_lines() -> None:
    existing = "# header\n\nA=1\n# trailing note\n"
    out = rsw.patch_env(existing, {"B": "2"})
    assert out.splitlines() == ["# header", "", "A=1", "# trailing note", "B=2"]


def test_patch_env_does_not_touch_commented_key_of_same_name() -> None:
    # a commented-out KEY line must not be treated as the live key
    existing = "# EXOMEM_BASE_URL=https://old\n"
    out = rsw.patch_env(existing, {"EXOMEM_BASE_URL": "https://new"})
    assert out.splitlines() == [
        "# EXOMEM_BASE_URL=https://old",
        "EXOMEM_BASE_URL=https://new",
    ]


def test_patch_env_empty_input_appends_all() -> None:
    out = rsw.patch_env("", {"A": "1", "B": "2"})
    assert out == "A=1\nB=2\n"


def test_patch_env_always_ends_with_single_newline() -> None:
    out = rsw.patch_env("A=1", {"B": "2"})  # existing had no trailing newline
    assert out.endswith("\n") and not out.endswith("\n\n")


# ============================================================================
# render_oauth_fields
# ============================================================================


def test_render_oauth_fields_contains_homepage_and_callback() -> None:
    base = "https://kb.example.com"
    text = rsw.render_oauth_fields(base)
    assert f"Homepage URL                 {base}" in text
    assert f"Authorization callback URL   {base}/auth/callback" in text
    assert "New OAuth App" in text


# ============================================================================
# run_remote_setup — orchestration (injected doctor_fn, no network / real .env)
# ============================================================================


def _run(env_path: Path, doctor_success: bool = True, **overrides):
    lines: list[str] = []
    doctor_calls: list[dict] = []

    def fake_doctor(**kw):
        doctor_calls.append(kw)
        return SimpleNamespace(success=doctor_success, profile="remote", checks=[])

    kwargs = dict(
        vault="/vault",
        base_url="https://kb.example.com",
        tunnel="ngrok",
        github_client_id="Iv1.abc",
        github_client_secret="secret-xyz",
        github_username="octocat",
        github_user_id="1234",
        yes=True,
        env_path=env_path,
        input_fn=lambda prompt="": pytest.fail(f"unexpected prompt: {prompt}"),
        print_fn=lines.append,
        doctor_fn=fake_doctor,
        load_env_fn=lambda p: None,
    )
    kwargs.update(overrides)
    code = rsw.run_remote_setup(**kwargs)
    return code, "\n".join(lines), doctor_calls


def test_happy_path_writes_env_and_prints_connector(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    code, out, doctor_calls = _run(env_path)
    assert code == 0

    written = rsw.parse_env(env_path.read_text(encoding="utf-8"))
    assert written["EXOMEM_VAULT_PATH"] == str(Path("/vault").expanduser())
    assert written["EXOMEM_BASE_URL"] == "https://kb.example.com"
    assert written["GITHUB_CLIENT_ID"] == "Iv1.abc"
    assert written["GITHUB_CLIENT_SECRET"] == "secret-xyz"
    assert written["EXOMEM_GITHUB_USERNAME"] == "octocat"
    assert written["EXOMEM_GITHUB_USER_ID"] == "1234"
    assert len(written["EXOMEM_JWT_SIGNING_KEY"]) >= 64  # freshly generated

    # doctor ran as the remote gate, with the live probe on
    assert doctor_calls == [{"vault": str(Path("/vault").expanduser()), "profile": "remote", "probe": True}]

    # the exact connector URL + OAuth callback were shown
    assert "https://kb.example.com/mcp" in out
    assert "https://kb.example.com/auth/callback" in out
    # ngrok cap flagged as UNVERIFIED
    assert "UNVERIFIED" in out and "120 req" in out


def test_existing_signing_key_is_preserved(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "EXOMEM_JWT_SIGNING_KEY=keep-this-stable-key\nUNRELATED=x\n", encoding="utf-8"
    )
    code, out, _ = _run(env_path)
    assert code == 0
    written = rsw.parse_env(env_path.read_text(encoding="utf-8"))
    assert written["EXOMEM_JWT_SIGNING_KEY"] == "keep-this-stable-key"
    assert written["UNRELATED"] == "x"  # unrelated key preserved
    assert "[skipped: already set" in out


def test_existing_github_user_id_is_preserved_without_network(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("EXOMEM_GITHUB_USER_ID=1234\n", encoding="utf-8")

    calls: list[str] = []

    def resolver(username: str):
        calls.append(username)
        return {"id": 1234, "login": "octocat"}

    code, _, _ = _run(
        env_path,
        github_user_id=None,
        github_user_resolver=resolver,
    )

    assert code == 0
    assert calls == ["octocat"]
    assert rsw.parse_env(env_path.read_text())["EXOMEM_GITHUB_USER_ID"] == "1234"


def test_existing_id_rejects_changed_login_resolving_to_another_account(
    tmp_path: Path,
) -> None:
    env_path = tmp_path / ".env"
    original = "EXOMEM_GITHUB_USERNAME=old-login\nEXOMEM_GITHUB_USER_ID=1234\n"
    env_path.write_text(original, encoding="utf-8")

    code, output, doctor_calls = _run(
        env_path,
        github_username="new-login",
        github_user_id=None,
        github_user_resolver=lambda _username: {"id": 9999, "login": "new-login"},
    )

    assert code == 2
    assert "identity" in output.lower()
    assert doctor_calls == []
    assert env_path.read_text() == original


def test_existing_id_accepts_online_resolution_of_same_account(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("EXOMEM_GITHUB_USER_ID=1234\n", encoding="utf-8")

    code, _, _ = _run(
        env_path,
        github_user_id=None,
        github_user_resolver=lambda _username: {"id": 1234, "login": "OctoCat"},
    )

    assert code == 0
    values = rsw.parse_env(env_path.read_text())
    assert values["EXOMEM_GITHUB_USER_ID"] == "1234"
    assert values["EXOMEM_GITHUB_USERNAME"] == "octocat"


def test_setup_resolves_numeric_id_and_requires_normalized_login_match(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    calls: list[str] = []

    def resolver(username: str):
        calls.append(username)
        return {"id": 1234, "login": "OctoCat"}

    code, _, _ = _run(
        env_path,
        github_user_id=None,
        github_user_resolver=resolver,
    )

    assert code == 0
    assert calls == ["octocat"]
    assert rsw.parse_env(env_path.read_text())["EXOMEM_GITHUB_USER_ID"] == "1234"


@pytest.mark.parametrize(
    ("github_user_id", "resolver_result"),
    [
        ("0", None),
        ("not-a-number", None),
        (None, {"id": 1234, "login": "someone-else"}),
        (None, {"id": None, "login": "octocat"}),
    ],
)
def test_invalid_or_mismatched_github_identity_fails_before_write(
    tmp_path: Path, github_user_id, resolver_result
) -> None:
    env_path = tmp_path / ".env"
    code, _, doctor_calls = _run(
        env_path,
        github_user_id=github_user_id,
        github_user_resolver=lambda _username: resolver_result,
    )

    assert code == 2
    assert doctor_calls == []
    assert not env_path.exists()


@pytest.mark.parametrize(
    ("existing", "expected"),
    [
        ("EXOMEM_WRITER_LEASE_TOKEN=writer-only\nEXOMEM_WRITER_LEASE_URL=https://c\nEXOMEM_OAUTH_STORAGE_URL=https://c\nEXOMEM_OAUTH_STORAGE_NAMESPACE=main\n", "writer-only"),
        ("EXOMEM_OAUTH_STORAGE_TOKEN=oauth-only\nEXOMEM_OAUTH_STORAGE_URL=https://c\nEXOMEM_WRITER_LEASE_URL=https://c\nEXOMEM_OAUTH_STORAGE_NAMESPACE=main\n", "oauth-only"),
        ("EXOMEM_WRITER_LEASE_URL=https://c\nEXOMEM_OAUTH_STORAGE_URL=https://c\nEXOMEM_OAUTH_STORAGE_NAMESPACE=main\n", None),
    ],
)
def test_ha_setup_preserves_or_generates_one_matching_storage_credential(
    tmp_path: Path, existing: str, expected: str | None
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(existing, encoding="utf-8")

    code, output, _ = _run(env_path)

    assert code == 0
    values = rsw.parse_env(env_path.read_text())
    assert values["EXOMEM_WRITER_LEASE_TOKEN"]
    assert values["EXOMEM_WRITER_LEASE_TOKEN"] == values["EXOMEM_OAUTH_STORAGE_TOKEN"]
    assert values["EXOMEM_WRITER_LEASE_TOKEN"] == values["EXOMEM_LEASE_COORDINATOR_TOKEN"]
    if expected is not None:
        assert values["EXOMEM_WRITER_LEASE_TOKEN"] == expected
    assert values["EXOMEM_WRITER_LEASE_TOKEN"] not in output


def test_ha_setup_conflicting_credentials_fail_before_any_write(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    original = (
        "EXOMEM_WRITER_LEASE_URL=https://c\n"
        "EXOMEM_OAUTH_STORAGE_URL=https://c\n"
        "EXOMEM_OAUTH_STORAGE_NAMESPACE=main\n"
        "EXOMEM_WRITER_LEASE_TOKEN=one\n"
        "EXOMEM_OAUTH_STORAGE_TOKEN=two\n"
    )
    env_path.write_text(original, encoding="utf-8")

    code, output, doctor_calls = _run(env_path)

    assert code == 2
    assert "conflict" in output.lower()
    assert doctor_calls == []
    assert env_path.read_text() == original


@pytest.mark.parametrize(
    "existing",
    [
        "EXOMEM_WRITER_LEASE_URL=https://c\nEXOMEM_WRITER_LEASE_VAULT_ID=main\n",
        "EXOMEM_WRITER_LEASE_URL=https://c\nEXOMEM_OAUTH_STORAGE_URL=https://c\n",
    ],
)
def test_ha_setup_requires_shared_session_url_and_namespace_before_write(
    tmp_path: Path, existing: str
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(existing, encoding="utf-8")

    code, output, doctor_calls = _run(env_path)

    assert code == 2
    assert "HA" in output
    assert doctor_calls == []
    assert env_path.read_text() == existing


def test_interactive_existing_client_secret_is_preserved_without_rendering_it(
    tmp_path: Path,
) -> None:
    env_path = tmp_path / ".env"
    secret = "existing-super-secret"
    env_path.write_text(f"GITHUB_CLIENT_SECRET={secret}\n", encoding="utf-8")
    prompts: list[str] = []
    output: list[str] = []

    def input_fn(prompt: str = "") -> str:
        prompts.append(prompt)
        return ""

    code = rsw.run_remote_setup(
        vault="/vault",
        base_url="https://kb.example.com",
        tunnel="ngrok",
        github_client_id="client-id",
        github_client_secret=None,
        github_username="octocat",
        github_user_id="1234",
        yes=False,
        probe=False,
        env_path=env_path,
        input_fn=input_fn,
        print_fn=output.append,
        doctor_fn=lambda **_kwargs: SimpleNamespace(success=True),
        load_env_fn=lambda _path: None,
    )

    assert code == 0
    assert rsw.parse_env(env_path.read_text())["GITHUB_CLIENT_SECRET"] == secret
    assert secret not in "\n".join(prompts + output)
    assert any("keep existing" in prompt.lower() for prompt in prompts)


def test_doctor_failure_is_a_hard_gate(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    code, out, _ = _run(env_path, doctor_success=False)
    assert code == 1
    # .env is still written (so the user can fix + rerun), but the connector
    # URL is NOT presented as ready
    assert env_path.exists()
    assert "https://kb.example.com/mcp" not in out
    assert "[failed" in out


def test_no_probe_flag_passes_through(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    _, out, doctor_calls = _run(env_path, probe=False)
    assert doctor_calls[0]["probe"] is False
    assert "offline" in out


def test_yes_mode_rejects_bad_base_url_before_writing(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    code, out, doctor_calls = _run(env_path, base_url="https://kb.example.com/mcp")
    assert code == 2
    assert doctor_calls == []  # never reached the gate
    assert not env_path.exists()  # nothing written


# ============================================================================
# dispatch + arg validation via `exomem setup --remote`
# ============================================================================


def test_setup_remote_dispatches_from_main(monkeypatch: pytest.MonkeyPatch) -> None:
    called: dict = {}
    monkeypatch.setattr(rsw, "run_remote_setup", lambda **kw: called.update(kw) or 0)
    code = main([
        "setup", "--remote",
        "--vault", "/v", "--base-url", "https://kb.example.com",
        "--tunnel", "cloudflare",
        "--github-client-id", "id", "--github-client-secret", "sec",
        "--github-username", "octocat", "--yes",
        "--github-user-id", "1234",
    ])
    assert code == 0
    assert called["base_url"] == "https://kb.example.com"
    assert called["tunnel"] == "cloudflare"
    assert called["probe"] is True
    assert called["github_user_id"] == "1234"


def test_setup_remote_yes_without_required_flags_is_usage_error() -> None:
    with pytest.raises(SystemExit) as e:
        main(["setup", "--remote", "--yes", "--vault", "/v"])
    assert e.value.code == 2
