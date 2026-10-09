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
    operator: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem.auth_sessions import secrets

    token_urlsafe = secrets.token_urlsafe
    session_ids = iter(("-synthetic-home-session", "synthetic-codex-session"))
    monkeypatch.setattr(
        secrets, "token_urlsafe", lambda n: next(session_ids) if n == 18 else token_urlsafe(n)
    )
    for client in ("home", "codex"):
        assert main(
            ["auth", "issue-local", "--client", client, "--output", str(operator / client), "--json"]
        ) == 0
    home_id = json.loads(capsys.readouterr().out.splitlines()[0])["session_id"]
    home = (operator / "home").read_text().strip()
    codex = (operator / "codex").read_text().strip()

    assert main(["auth", "revoke", "--", home_id]) == 0
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


def test_attach_asks_for_an_owner_only_original_and_a_hold_refuses_the_flag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A dropped flag stores a private export as an ordinary, releasable original."""
    source = tmp_path / "export.zip"
    source.write_bytes(b"PK private export")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        request.read()
        seen.append(request)
        return httpx.Response(201, json={"path": "Evidence/Health/Exports/raw.export.zip"})

    token = ["--token-file", str(_token_file(tmp_path)), "--port", "8764"]
    transport = httpx.MockTransport(handler)
    code = _attach_main(
        [str(source), "--scope", "Health", "--category", "Exports", "--raw-protection", *token],
        transport=transport,
    )

    assert code == 0
    assert b'name="raw_protection"\r\n\r\n1\r\n' in seen[0].content
    with pytest.raises(SystemExit) as exc:
        _attach_main([str(source), "--raw-protection", *token], transport=transport)
    assert exc.value.code == 2 and len(seen) == 1
    capsys.readouterr()


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


class _ServiceTransport(httpx.BaseTransport):
    """The real service app behind the CLI's sync client; `cut` truncates one part.

    A cut part delivers its first `cut` bytes to the service and then fails on
    the client side, as a dropped connection does.
    """

    def __init__(self, app, *, cut: int | None = None) -> None:
        self.app = app
        self.cut = cut
        self.parts: list[int] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        body = request.read()
        dropped = request.method == "PATCH" and self.cut is not None
        if dropped:
            body, self.cut = body[: self.cut], None
        if request.method == "PATCH":
            self.parts.append(len(body))
        headers = [(k, v) for k, v in request.headers.raw if k.lower() != b"content-length"]

        async def forward() -> httpx.Response:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app)) as client:
                return await client.request(request.method, str(request.url), headers=headers, content=body)

        response = asyncio.run(forward())
        if dropped:
            raise httpx.WriteError("connection dropped", request=request)
        return httpx.Response(response.status_code, headers=response.headers, content=response.content)


def test_attach_resumes_an_interrupted_archive_upload_on_its_next_run(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import hashlib
    import io
    import zipfile

    from exomem import server, server_transfer
    from exomem.state_paths import state_store_root

    monkeypatch.setattr(server, "load_dotenv", lambda *a, **k: None)

    async def inline_threadpool(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(server_transfer, "run_in_threadpool", inline_threadpool)
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-state"))
    # The listener authorizes the CLI's bearer as it would a local client token.
    monkeypatch.setenv("EXOMEM_UPLOAD_TOKEN", "exo_s1.synthetic-local-token")
    app = server.build_server(require_auth=False).http_app()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for day in range(1, 4):
            archive.writestr(f"device_days/day-{day}.json", f'{{"heart_rate": [{day}, 60, 61]}}' * 50)
    source = tmp_path / "export.zip"
    source.write_bytes(buffer.getvalue())
    argv = [str(source), "--scope", "Device", "--category", "Exports", "--archive", "members",
            "--resumable", "--token-file", str(_token_file(tmp_path)), "--port", "8764"]
    records = state_store_root() / "attach-sessions"

    interrupted = _ServiceTransport(app, cut=len(source.read_bytes()) // 2)
    assert _attach_main(argv, transport=interrupted) == 1
    assert "run the same command again" in capsys.readouterr().err
    [record] = records.glob("*.json")
    assert stat.S_IMODE(record.stat().st_mode) == 0o600
    assert "synthetic-local-token" not in record.read_text()

    resumed = _ServiceTransport(app)
    assert _attach_main(argv, transport=resumed) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert resumed.parts == [len(source.read_bytes()) - interrupted.parts[0]]
    assert receipt["archive"]["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    manifest = json.loads((vault / receipt["path"]).read_bytes())
    assert manifest["archive"]["verified"] == "session"
    assert [member["path"] for member in manifest["members"]] == [
        f"device_days/day-{day}.json" for day in range(1, 4)
    ]
    assert list(records.glob("*.json")) == []
