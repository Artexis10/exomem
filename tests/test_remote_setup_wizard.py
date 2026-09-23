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

import os
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


def test_default_env_path_is_service_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    assert rsw._default_env_path() == tmp_path / ".env"


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
        ("EXOMEM_WRITER_LEASE_TOKEN=writer-only\nEXOMEM_WRITER_LEASE_URL=https://c\nEXOMEM_WRITER_LEASE_VAULT_ID=main\nEXOMEM_WRITER_LEASE_REPLICA_ID=replica-a\nEXOMEM_OAUTH_STORAGE_URL=https://c\nEXOMEM_OAUTH_STORAGE_NAMESPACE=main\n", "writer-only"),
        ("EXOMEM_OAUTH_STORAGE_TOKEN=oauth-only\nEXOMEM_OAUTH_STORAGE_URL=https://c\nEXOMEM_WRITER_LEASE_URL=https://c\nEXOMEM_WRITER_LEASE_VAULT_ID=main\nEXOMEM_WRITER_LEASE_REPLICA_ID=replica-a\nEXOMEM_OAUTH_STORAGE_NAMESPACE=main\n", "oauth-only"),
        ("EXOMEM_WRITER_LEASE_URL=https://c\nEXOMEM_WRITER_LEASE_VAULT_ID=main\nEXOMEM_WRITER_LEASE_REPLICA_ID=replica-a\nEXOMEM_OAUTH_STORAGE_URL=https://c\nEXOMEM_OAUTH_STORAGE_NAMESPACE=main\n", None),
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
        "EXOMEM_WRITER_LEASE_VAULT_ID=main\n"
        "EXOMEM_WRITER_LEASE_REPLICA_ID=replica-a\n"
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


@pytest.mark.parametrize(
    "existing",
    [
        (
            "EXOMEM_WRITER_LEASE_URL=https://c\n"
            "EXOMEM_OAUTH_STORAGE_URL=https://c\n"
            "EXOMEM_OAUTH_STORAGE_NAMESPACE=main\n"
        ),
        (
            "EXOMEM_WRITER_LEASE_URL=https://c\n"
            "EXOMEM_WRITER_LEASE_VAULT_ID=main\n"
            "EXOMEM_OAUTH_STORAGE_URL=https://c\n"
        ),
    ],
)
def test_ha_setup_requires_complete_writer_topology_before_write(
    tmp_path: Path, existing: str
) -> None:
    env_path = tmp_path / ".env"
    original = existing.encode()
    env_path.write_bytes(original)

    code, output, doctor_calls = _run(env_path)

    assert code == 2
    assert "HA" in output
    assert doctor_calls == []
    assert env_path.read_bytes() == original


@pytest.mark.parametrize(
    "existing",
    [
        "EXOMEM_WRITER_LEASE_TOKEN=token-only\n",
        "EXOMEM_LEASE_COORDINATOR_TOKEN=token-only\n",
        "EXOMEM_OAUTH_STORAGE_TOKEN=token-only\n",
        "EXOMEM_OAUTH_STORAGE_NAMESPACE=namespace-only\n",
        "EXOMEM_WRITER_LEASE_VAULT_ID=vault-only\n",
        "EXOMEM_HA_REPLICA_URLS=https://replica.example\n",
    ],
)
def test_partial_ha_signal_fails_before_writing_env(
    tmp_path: Path, existing: str
) -> None:
    env_path = tmp_path / ".env"
    original = existing.encode()
    env_path.write_bytes(original)

    code, output, doctor_calls = _run(env_path)

    assert code == 2
    assert "HA" in output
    assert doctor_calls == []
    assert env_path.read_bytes() == original


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


# ============================================================================
# the remote owner binding (EXOMEM_OWNER_OAUTH_SUBJECT)
# ============================================================================

OWNER_KEY = "EXOMEM_OWNER_OAUTH_SUBJECT"


def test_patch_env_none_removes_a_key_and_keeps_the_rest() -> None:
    text = "A=1\n# note\nEXOMEM_OWNER_OAUTH_SUBJECT=github:1234\nB=2\n"
    assert rsw.patch_env(text, {OWNER_KEY: None}) == "A=1\n# note\nB=2\n"
    assert rsw.patch_env("A=1\n", {OWNER_KEY: None}) == "A=1\n"


def test_yes_without_a_flag_writes_no_binding(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    code, _, _ = _run(env_path)
    assert code == 0
    assert OWNER_KEY not in rsw.parse_env(env_path.read_text(encoding="utf-8"))


def test_yes_without_a_flag_leaves_an_existing_binding_alone(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(f"{OWNER_KEY}=github:1234\n", encoding="utf-8")
    code, _, _ = _run(env_path)
    assert code == 0
    assert rsw.parse_env(env_path.read_text(encoding="utf-8"))[OWNER_KEY] == "github:1234"


def test_remote_owner_flag_binds_the_resolved_id(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    code, out, _ = _run(env_path, remote_owner=True)
    assert code == 0
    assert rsw.parse_env(env_path.read_text(encoding="utf-8"))[OWNER_KEY] == "github:1234"
    assert "remote_owner" in out


def test_no_remote_owner_flag_removes_the_binding(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(f"KEEP=1\n{OWNER_KEY}=github:1234\n", encoding="utf-8")
    code, _, _ = _run(env_path, remote_owner=False)
    assert code == 0
    written = rsw.parse_env(env_path.read_text(encoding="utf-8"))
    assert OWNER_KEY not in written
    assert written["KEEP"] == "1"


@pytest.mark.parametrize(
    ("answer", "bound"), [("", True), ("y", True), ("Yes", True), ("n", False), ("no", False)]
)
def test_interactive_setup_asks_whether_the_account_is_the_owner(
    tmp_path: Path, answer: str, bound: bool
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        f"GITHUB_CLIENT_SECRET=kept\n{OWNER_KEY}=github:1234\n", encoding="utf-8"
    )
    prompts: list[str] = []

    def input_fn(prompt: str = "") -> str:
        prompts.append(prompt)
        return answer if "owner" in prompt else ""

    code, _, _ = _run(env_path, yes=False, probe=False, input_fn=input_fn)
    assert code == 0
    owner_prompts = [prompt for prompt in prompts if "owner" in prompt]
    assert len(owner_prompts) == 1
    assert "octocat" in owner_prompts[0]
    assert "act as you, the owner" in owner_prompts[0]
    assert "[Y/n]" in owner_prompts[0]
    written = rsw.parse_env(env_path.read_text(encoding="utf-8"))
    assert (written.get(OWNER_KEY) == "github:1234") is bound
    assert (OWNER_KEY in written) is bound


_OWNER_FLAG_BASE = [
    "setup", "--remote",
    "--vault", "/v", "--base-url", "https://kb.example.com",
    "--tunnel", "cloudflare",
    "--github-client-id", "id", "--github-client-secret", "sec",
    "--github-username", "octocat", "--yes",
]


@pytest.mark.parametrize(
    ("extra", "expected"),
    [([], None), (["--remote-owner"], True), (["--no-remote-owner"], False)],
)
def test_setup_remote_dispatches_the_owner_flags(
    monkeypatch: pytest.MonkeyPatch, extra: list[str], expected: bool | None
) -> None:
    called: dict = {}
    monkeypatch.setattr(rsw, "run_remote_setup", lambda **kw: called.update(kw) or 0)
    assert main([*_OWNER_FLAG_BASE, *extra]) == 0
    assert called["remote_owner"] is expected


def test_setup_remote_owner_flags_are_exclusive() -> None:
    with pytest.raises(SystemExit) as e:
        main([*_OWNER_FLAG_BASE, "--remote-owner", "--no-remote-owner"])
    assert e.value.code == 2


def test_parse_env_reads_export_prefixed_lines_like_dotenv() -> None:
    parsed = rsw.parse_env("export A=1\n  export   B=2\nexporter=3\n# export C=4\n")
    assert parsed == {"A": "1", "B": "2", "exporter": "3"}


def test_patch_env_replaces_and_removes_export_prefixed_lines() -> None:
    text = "KEEP=1\nexport EXOMEM_OWNER_OAUTH_SUBJECT=github:1234\nexport OTHER=x\n"
    assert rsw.patch_env(text, {OWNER_KEY: None}) == "KEEP=1\nexport OTHER=x\n"
    assert rsw.patch_env(text, {OWNER_KEY: "github:5678"}) == (
        "KEEP=1\nexport EXOMEM_OWNER_OAUTH_SUBJECT=github:5678\nexport OTHER=x\n"
    )


@pytest.mark.parametrize("separator", [" ", "\t", " \t ", "\t\t"])
def test_patch_env_removes_an_export_line_with_any_whitespace(separator: str) -> None:
    text = f"KEEP=1\nexport{separator}{OWNER_KEY}=github:1234\n"
    assert rsw.parse_env(text) == {"KEEP": "1", OWNER_KEY: "github:1234"}
    assert rsw.patch_env(text, {OWNER_KEY: None}) == "KEEP=1\n"


def test_no_remote_owner_removes_an_export_prefixed_binding(tmp_path: Path) -> None:
    """Removal must hold for python-dotenv too, which reads `export KEY=`."""
    from dotenv import dotenv_values

    env_path = tmp_path / ".env"
    env_path.write_text(f"KEEP=1\nexport {OWNER_KEY}=github:1234\n", encoding="utf-8")
    code, _, _ = _run(env_path, remote_owner=False)
    assert code == 0
    assert OWNER_KEY not in env_path.read_text(encoding="utf-8")
    assert OWNER_KEY not in dotenv_values(env_path)
    assert dotenv_values(env_path)["KEEP"] == "1"


def test_the_doctor_gate_does_not_see_a_removed_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A binding inherited from the shell or service must not outlive its
    removal from the file when the wizard's own doctor gate runs."""
    from exomem.governance.principal import remote_owner_binding_state

    env_path = tmp_path / ".env"
    env_path.write_text(f"{OWNER_KEY}=github:1234\n", encoding="utf-8")
    monkeypatch.setenv(OWNER_KEY, "github:1234")
    seen: dict[str, str] = {}

    def doctor(**_kwargs):
        seen["state"] = remote_owner_binding_state()
        return SimpleNamespace(success=True, profile="remote", checks=[])

    code, _, _ = _run(env_path, remote_owner=False, doctor_fn=doctor, load_env_fn=rsw._load_env)
    assert code == 0
    assert seen["state"] == "unset"
    assert OWNER_KEY not in os.environ
