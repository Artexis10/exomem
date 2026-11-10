"""An agent without a shell imports a folder by sending one archive and repeating one call until it is filed.

Every case runs the published tools, the real upload receiver, the real unpacker and a real vault. Only the
session identity is synthetic, as in `test_agent_upload_handoff.py`; the receiver needs one, and a stdio
session has none.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import tarfile
import zipfile
from pathlib import Path

import httpx
import pytest
from fastmcp import Client, FastMCP
from test_adoption_paging import PDF

from exomem import (
    adoption_run,
    archive_members,
    auth_sessions,
    command_surface,
    server,
    server_auth,
    server_transfer,
    writer_lease,
)
from exomem.governance import principal

PAGE = 2


@pytest.fixture
def agent(vault, tmp_path, monkeypatch):
    """A vault, a session that may hold uploads, and an upload receiver; `agent.mcp` builds the tool surface."""
    monkeypatch.setattr(adoption_run, "MAX_CANDIDATES_DEFAULT", PAGE)
    monkeypatch.setattr(server, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setenv("EXOMEM_BASE_URL", "https://capture.example")
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-lease"))
    monkeypatch.delenv("EXOMEM_UPLOAD_TOKEN", raising=False)
    monkeypatch.delenv("EXOMEM_CF_ACCESS_TEAM_DOMAIN", raising=False)
    authority = auth_sessions.SessionAuthority.local(
        directory=tmp_path / "sessions", signing_root="synthetic-private-signing-key",
        issuer="https://capture.example", audience="https://capture.example/mcp")
    monkeypatch.setattr(server_auth, "build_session_authority", lambda **_: authority)
    _, record = asyncio.run(authority.issue(
        client_id="agent", scopes=["mcp"],
        identity=auth_sessions.SessionIdentity(github_user_id=42, github_login="example")))
    who = principal.RequestPrincipal(
        audience_id="owner", surface="mcp", issuer_family="mcp-oauth:synthetic", remote_owner=True,
        client_binding=principal.ClientBinding(authority.issuer, record.client_id),
        origin_session=principal.OriginSessionBinding(record.session_id, record.generation, record.audience))
    monkeypatch.setattr(principal, "resolve_mcp_principal", lambda: who)

    async def inline(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(server_transfer, "run_in_threadpool", inline)
    receiver = FastMCP("import-upload-test")
    server_transfer.register_transfer_routes(receiver, vault_root=vault, media_worker=None)
    yield _Agent(vault, receiver.http_app())
    writer_lease.reset_managers_for_tests()


class _Agent:
    """What an agent does: ask for an upload, send bytes to the URL it was given, call `manage_memory`."""

    def __init__(self, vault: Path, receiver) -> None:
        self.vault = vault
        self.receiver = receiver
        self.calls = 0

    def mcp(self) -> Client:
        return Client(server.build_server(require_auth=False))

    async def ask(self, client: Client, archive: bytes) -> dict:
        """The `upload_files` reply for lane import."""
        result = await client.call_tool(
            "upload_files", {"lane": "import", "expected_sha256": hashlib.sha256(archive).hexdigest()},
            raise_on_error=False)
        assert not result.is_error, result
        return result.structured_content

    async def send(self, step: dict, archive: bytes, *, name: str = "folder.bin") -> httpx.Response:
        """The multipart POST that the reply's `send` step spells out, made through the real receiver."""
        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.receiver), base_url="https://capture.example") as http:
            return await http.post(
                "/upload", headers={"Authorization": "Bearer " + step["token"]},
                files={"file": (name, archive, "application/octet-stream")},
                data={"lane": "import", "hold": "1"})

    async def file(self, client: Client, **arguments) -> dict:
        """One `imports.file` call under its own idempotency key; returns the reply, or `{"refused": "CODE: message"}`."""
        self.calls += 1
        result = await client.call_tool(
            "manage_memory", {"operation": "imports.file", "arguments": arguments}, raise_on_error=False,
            meta={command_surface.MCP_IDEMPOTENCY_META_KEY: f"import-upload-{self.calls}"})
        assert not result.is_error, result
        reply = result.structured_content
        if "error" in reply:
            return {"refused": f"{reply['error']['code']}: {reply['error']['message']}"}
        return reply

    async def file_to_done(self, client: Client, token: str) -> list[dict]:
        """Repeat the call the way the reply tells the agent to, until it ends; returns every reply."""
        replies: list[dict] = []
        arguments = {"upload": token}
        for _ in range(40):
            reply = await self.file(client, **arguments)
            assert "refused" not in reply, reply
            replies.append(reply)
            if reply["filing"] != "working":
                return replies
            arguments = {"upload": token, "continuation": reply["continuation"]}
        raise AssertionError("the agent repeated the call 40 times and filing never ended")

    def imports(self) -> list[str]:
        folder = self.vault / "_Imports"
        return sorted(path.name for path in folder.iterdir()) if folder.exists() else []

    def sources(self) -> list[Path]:
        return sorted(path for path in (self.vault / "Knowledge Base" / "Sources").rglob("*.md") if path.name != "index.md")

    def runs(self) -> list[Path]:
        return sorted(path for path in adoption_run.AdoptionRunStore(self.vault).base.glob("*") if path.is_dir())


def _tar(files: dict[str, bytes], *, extra: tuple[tarfile.TarInfo, ...] = ()) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        for info in extra:
            archive.addfile(info)
    return buffer.getvalue()


def _zip(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def _link(name: str, kind: bytes, target: str) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type, info.linkname = kind, target
    return info


@pytest.mark.parametrize("pack", [_tar, _zip], ids=["tar", "zip"])
def test_an_agent_imports_a_folder_by_sending_one_archive_and_repeating_one_call_until_done(
        agent, duplicate_index, pack):
    """Catches a route that cannot reach `done` from the descriptor alone, or that files a repeated file twice."""
    # Seven files, five byte strings: b.pdf repeats a.pdf in another folder, e.pdf repeats d.pdf.
    folder = {"a.pdf": PDF + b"a", "docs/b.pdf": PDF + b"a", "docs/c.pdf": PDF + b"c", "docs/deep/d.pdf": PDF + b"d",
              "e.pdf": PDF + b"d", "f.pdf": PDF + b"f", "g.pdf": PDF + b"g"}
    unique = {hashlib.sha256(data).hexdigest() for data in folder.values()}
    before = agent.sources()
    archive = pack(folder)

    async def journey():
        async with agent.mcp() as client:
            step = await agent.ask(client, archive)
            sent = await agent.send(step, archive)
            assert sent.status_code == 201, sent.text
            # The agent follows the reply's own `import` step; it knows nothing else about the upload.
            assert step["next"][1]["tool"] == "manage_memory"
            token = step["next"][1]["arguments"]["arguments"]["upload"]
            return await agent.file_to_done(client, token)

    replies = asyncio.run(journey())

    done = replies[-1]
    assert done["filing"] == "done" and done["progress"]["failed"] == 0
    assert done["progress"]["skipped_duplicates"] == len(folder) - len(unique)
    assert done["progress"]["filed"] == len(unique)
    assert len(replies) > 1, "the page size should make the import take several calls"
    assert len({reply["path"] for reply in replies}) == 1 and replies[0]["path"].startswith("_Imports/")
    assert len(agent.sources()) - len(before) == len(unique)
    stored = {hashlib.sha256(path.read_bytes()).hexdigest() for path in (agent.vault / "Knowledge Base").rglob("*.pdf")}
    assert unique <= stored


def test_a_repeated_first_call_resumes_the_same_import_and_unpacks_nothing_again(agent, duplicate_index):
    """Catches a retry whose first reply was lost that unpacks the archive again, or scans the folder a second time."""
    archive = _tar({f"doc{n}.pdf": PDF + bytes([n]) for n in range(5)})

    async def journey():
        async with agent.mcp() as client:
            step = await agent.ask(client, archive)
            assert (await agent.send(step, archive)).status_code == 201
            token = step["token"]
            first = await agent.file(client, upload=token)
            again = await agent.file(client, upload=token)  # a new key: the first reply was lost on the way
            replies = [first, again]
            while replies[-1]["filing"] == "working":  # carried on by path, as the reply's `path` allows
                replies.append(await agent.file(client, path=first["path"], continuation=replies[-1]["continuation"]))
                assert len(replies) < 40
            replies.append(await agent.file(client, upload=token))  # the reply to the last call was lost
            return replies

    replies = asyncio.run(journey())

    assert replies[0]["filing"] == "working" and replies[1]["run_id"] == replies[0]["run_id"]
    for done in replies[-2:]:  # the last call, and its repeat after filing removed the stage
        assert done["filing"] == "done" and done["progress"]["filed"] == 5 and done["progress"]["skipped_duplicates"] == 0
    assert len(agent.runs()) == 3  # five files at two a page, scanned once


def test_an_upload_and_a_path_together_are_refused(agent):
    """Catches a call that names two folders and files whichever one the server happens to read."""

    async def journey():
        async with agent.mcp() as client:
            return await agent.file(client, upload="exu_" + "0" * 64, path="_Imports/anything")

    reply = asyncio.run(journey())

    assert "IMPORT_REQUEST_INVALID" in reply["refused"]
    assert agent.imports() == []


def _zeros() -> dict[str, bytes]:
    """Two members that each fit the 2,000-byte cap of the cases below and together do not; zeros compress to nothing."""
    return {"first.bin": bytes(1_500), "second.bin": bytes(1_500)}


@pytest.mark.parametrize(("name", "archive", "code", "member"), [
    ("tar traversal", lambda: _tar({"ok.pdf": PDF, "../escape.pdf": PDF}), "ARCHIVE_MEMBER_REFUSED", "../escape.pdf"),
    ("tar absolute path", lambda: _tar({"/etc/escape.pdf": PDF}), "ARCHIVE_MEMBER_REFUSED", "/etc/escape.pdf"),
    ("tar symlink", lambda: _tar({"ok.pdf": PDF}, extra=(_link("link.pdf", tarfile.SYMTYPE, "/etc/passwd"),)),
     "ARCHIVE_MEMBER_REFUSED", "link.pdf"),
    ("tar hardlink", lambda: _tar({"ok.pdf": PDF}, extra=(_link("hard.pdf", tarfile.LNKTYPE, "ok.pdf"),)),
     "ARCHIVE_MEMBER_REFUSED", "hard.pdf"),
    ("tar device", lambda: _tar({}, extra=(_link("dev", tarfile.CHRTYPE, ""),)), "ARCHIVE_MEMBER_REFUSED", "dev"),
    ("tar too many entries", lambda: _tar({f"f{n}.pdf": PDF for n in range(4)}), "ARCHIVE_TOO_LARGE", None),
    ("tar expands past the cap", lambda: _tar(_zeros()), "ARCHIVE_TOO_LARGE", "second.bin"),
    ("zip traversal", lambda: _zip({"ok.pdf": PDF, "../escape.pdf": PDF}), "ARCHIVE_MEMBER_REFUSED", "../escape.pdf"),
    ("zip expands past the cap", lambda: _zip(_zeros()), "ARCHIVE_TOO_LARGE", None),
    ("not an archive", lambda: b"just some text, sent as if it were a folder", "ARCHIVE_UNREADABLE", None),
])
def test_an_archive_that_breaks_a_limit_is_refused_by_name_and_leaves_no_stage(
        agent, monkeypatch, name, archive, code, member):
    """Catches a refusal that leaves a partial `_Imports` stage, writes outside it, or keeps the archive held."""
    monkeypatch.setenv("EXOMEM_IMPORT_MAX_EXPANDED_BYTES", "2000")
    monkeypatch.setattr(archive_members, "MAX_ENTRIES", 3)
    body = archive()

    async def journey():
        async with agent.mcp() as client:
            step = await agent.ask(client, body)
            assert (await agent.send(step, body)).status_code == 201
            token = step["token"]
            return await agent.file(client, upload=token), await agent.file(client, upload=token)

    first, second = asyncio.run(journey())

    assert code in first["refused"], f"{name}: {first}"
    if member:
        assert member in first["refused"]
    assert agent.imports() == [], f"{name}: the refusal left {agent.imports()}"
    assert not (agent.vault / "escape.pdf").exists() and not (agent.vault / "_Imports" / "escape.pdf").exists()
    # The bytes would be refused again, so the archive is not kept for a retry.
    assert "IMPORT_UPLOAD_UNAVAILABLE" in second["refused"]


def test_an_archive_that_does_not_fit_stops_the_import_and_unpacks_once_there_is_room(agent, duplicate_index, monkeypatch):
    """Catches a full volume that is reported as a refused archive, or that drops the hold the retry needs."""
    archive = _tar({f"doc{n}.pdf": PDF + bytes([n]) for n in range(3)})
    monkeypatch.setenv("EXOMEM_IMPORT_RESERVE_BYTES", str(1 << 60))

    async def journey():
        async with agent.mcp() as client:
            step = await agent.ask(client, archive)
            assert (await agent.send(step, archive)).status_code == 201
            token = step["token"]
            stopped = await agent.file(client, upload=token)
            monkeypatch.setenv("EXOMEM_IMPORT_RESERVE_BYTES", "0")
            return stopped, await agent.file_to_done(client, token)

    stopped, replies = asyncio.run(journey())

    assert stopped["filing"] == "stopped" and stopped["stop"]["code"] == "INSUFFICIENT_SPACE"
    assert [item["need"] for item in stopped["person"]] == ["free_space"]
    assert replies[-1]["filing"] == "done" and replies[-1]["progress"]["filed"] == 3
