"""Operator CLI for local client tokens and the attach helper.

`exomem auth issue-local` mints a local-ingress session into an exclusive 0600
file and never prints the token; `exomem auth sessions`, `revoke <id>` and
`revoke --all` cover those sessions; `exomem attach` sends bytes, never a
path, to the local listener. All tokens and ids are synthetic.
"""

from __future__ import annotations

import asyncio
import json
import stat
from pathlib import Path

import httpx
import pytest

from exomem import local_ingress, server_auth
from exomem.__main__ import _attach_main, main

OWNER_ID = 4242


@pytest.fixture
def operator(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The service configuration an operator shell carries."""
    from fastmcp import settings

    monkeypatch.setattr("dotenv.load_dotenv", lambda **_kwargs: None)
    monkeypatch.setattr(settings, "home", tmp_path / "fastmcp-home")
    for name in ("EXOMEM_OAUTH_STORAGE_URL", "EXOMEM_WRITER_LEASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("EXOMEM_BASE_URL", "https://memory.example")
    monkeypatch.setenv("EXOMEM_JWT_SIGNING_KEY", "cli-signing-root")
    monkeypatch.setenv("EXOMEM_GITHUB_USER_ID", str(OWNER_ID))
    monkeypatch.setenv("EXOMEM_GITHUB_USERNAME", "example-owner")
    return tmp_path


def _validate(token: str):
    return asyncio.run(server_auth.build_local_session_authority().validate(token))


def test_issue_local_writes_an_owner_only_file_and_never_prints_the_token(
    operator: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = operator / "home.token"

    assert main(["auth", "issue-local", "--client", "home", "--output", str(output), "--json"]) == 0

    printed = capsys.readouterr()
    token = output.read_text().strip()
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert token and token not in printed.out and token not in printed.err
    result = json.loads(printed.out)
    assert result == {
        "session_id": result["session_id"],
        "client_id": "home",
        "ingress": "local",
        "output": str(output),
    }
    record = _validate(token)
    assert record is not None
    assert record.client_id == "home"
    assert record.audience == local_ingress.LOCAL_AUDIENCE
    assert record.expires_at is None
    # The public authority refuses it: the audience is the ingress.
    public = server_auth.build_session_authority(base_url="https://memory.example")
    assert asyncio.run(public.validate(token)) is None


def test_auth_sessions_lists_local_sessions_by_ingress(
    operator: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["auth", "issue-local", "--client", "home", "--output", str(operator / "t")]) == 0
    capsys.readouterr()

    assert main(["auth", "sessions", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)["sessions"]
    assert [(row["client_id"], row["ingress"], row["owner_equivalent"]) for row in rows] == [
        ("home", "local", True)
    ]
    assert main(["auth", "sessions"]) == 0
    assert capsys.readouterr().out.split()[2] == "owner-local"


def test_revoke_one_and_revoke_all_end_local_sessions(
    operator: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for client in ("home", "codex"):
        assert main(
            ["auth", "issue-local", "--client", client, "--output", str(operator / client), "--json"]
        ) == 0
    home_id = json.loads(capsys.readouterr().out.splitlines()[0])["session_id"]
    home = (operator / "home").read_text().strip()
    codex = (operator / "codex").read_text().strip()

    assert main(["auth", "revoke", home_id]) == 0
    assert _validate(home) is None
    assert _validate(codex) is not None
    assert main(["auth", "revoke", "--all"]) == 0
    assert _validate(codex) is None


def test_issue_local_refuses_an_existing_output_and_issues_nothing(
    operator: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    existing = operator / "existing.token"
    existing.write_text("keep me\n")
    link = operator / "link.token"
    link.symlink_to(operator / "elsewhere")

    for target in (existing, link):
        assert main(["auth", "issue-local", "--client", "home", "--output", str(target)]) == 2
    assert existing.read_text() == "keep me\n"
    assert not (operator / "elsewhere").exists()
    assert asyncio.run(server_auth.build_local_session_authority().list_sessions()) == []


def test_a_token_file_that_cannot_be_written_leaves_no_usable_session(
    operator: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = operator / "missing-directory" / "home.token"

    assert main(["auth", "issue-local", "--client", "home", "--output", str(output)]) == 1

    assert "revoked" in capsys.readouterr().err
    sessions = asyncio.run(server_auth.build_local_session_authority().list_sessions())
    assert [(s.status, s.revocation_reason) for s in sessions] == [
        ("revoked", "issue-local-write-failed")
    ]


@pytest.mark.parametrize("client", ["", "Home", "home token", "../home", "-home", "x" * 65])
def test_issue_local_rejects_a_client_label_that_is_not_a_plain_identifier(
    operator: Path, client: str
) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["auth", "issue-local", "--client", client, "--output", str(operator / "t")])
    assert exc.value.code == 2


def test_issue_local_needs_the_owner_identity(
    operator: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("EXOMEM_GITHUB_USERNAME")

    assert main(["auth", "issue-local", "--client", "home", "--output", str(operator / "t")]) == 2
    assert "EXOMEM_GITHUB_USERNAME" in capsys.readouterr().err
    assert not (operator / "t").exists()


# ---- exomem attach -----------------------------------------------------------


def _token_file(tmp_path: Path) -> Path:
    path = tmp_path / "home.token"
    path.write_text("exo_s1.synthetic-local-token\n")
    return path


def test_attach_sends_the_bytes_to_the_local_listener_and_prints_the_handle(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "notes.pdf"
    source.write_bytes(b"%PDF local bytes")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        request.read()
        seen.append(request)
        return httpx.Response(201, json={"path": "Evidence/Local/Attachments/notes.pdf"})

    code = _attach_main(
        [
            str(source),
            "--scope",
            "Local",
            "--category",
            "Attachments",
            "--token-file",
            str(_token_file(tmp_path)),
            "--port",
            "8764",
        ],
        transport=httpx.MockTransport(handler),
    )

    assert code == 0
    assert json.loads(capsys.readouterr().out) == {"path": "Evidence/Local/Attachments/notes.pdf"}
    request = seen[0]
    assert str(request.url) == "http://127.0.0.1:8764/upload"
    assert request.headers["authorization"] == "Bearer exo_s1.synthetic-local-token"
    body = request.content
    assert b"%PDF local bytes" in body
    assert b'name="scope"' in body and b"Local" in body
    # Bytes, never a path: the caller's filesystem location is not sent.
    assert str(tmp_path).encode() not in body


def test_attach_reads_its_token_and_port_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "a.txt"
    source.write_text("hello")
    monkeypatch.setenv("EXOMEM_LOCAL_TOKEN_FILE", str(_token_file(tmp_path)))
    monkeypatch.setenv("EXOMEM_LOCAL_PORT", "9123")
    urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        return httpx.Response(201, json={"path": "Evidence/a.txt"})

    assert _attach_main([str(source)], transport=httpx.MockTransport(handler)) == 0
    assert urls == ["http://127.0.0.1:9123/upload"]


@pytest.mark.parametrize(
    ("status", "body"),
    [(401, {"error": "invalid_token"}), (403, {"error": "local_ingress_refused"}), (500, None)],
)
def test_attach_reports_a_refusal_without_echoing_the_token(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], status: int, body: object
) -> None:
    source = tmp_path / "a.txt"
    source.write_text("hello")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body) if body else httpx.Response(status)

    code = _attach_main(
        [str(source), "--token-file", str(_token_file(tmp_path)), "--port", "8764"],
        transport=httpx.MockTransport(handler),
    )
    printed = capsys.readouterr()
    assert code == 1
    assert f"HTTP {status}" in printed.err
    assert "synthetic-local-token" not in printed.err + printed.out


def test_attach_reports_an_unreachable_listener(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "a.txt"
    source.write_text("hello")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    code = _attach_main(
        [str(source), "--token-file", str(_token_file(tmp_path)), "--port", "8764"],
        transport=httpx.MockTransport(handler),
    )
    assert code == 1
    assert "unreachable" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [
        ["a.txt", "--port", "8764"],
        ["a.txt", "--token-file", "t", "--port", "0"],
        ["a.txt", "--token-file", "t", "--port", "port"],
        ["a.txt", "--token-file", "t"],
    ],
)
def test_attach_needs_a_token_file_and_a_valid_port(
    argv: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EXOMEM_LOCAL_TOKEN_FILE", raising=False)
    monkeypatch.delenv("EXOMEM_LOCAL_PORT", raising=False)
    with pytest.raises(SystemExit) as exc:
        _attach_main(argv)
    assert exc.value.code == 2


def test_attach_is_a_cli_only_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from exomem.__main__ import _CLI_ONLY_SUBCOMMANDS

    assert "attach" in _CLI_ONLY_SUBCOMMANDS
    monkeypatch.delenv("EXOMEM_LOCAL_TOKEN_FILE", raising=False)
    with pytest.raises(SystemExit):
        main(["attach", str(tmp_path / "missing.txt")])
