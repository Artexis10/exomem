"""An agent imports a folder by sending one archive and repeating one call until it is filed.

Every case runs the published tools, the real upload receiver, the real unpacker and a real vault. The send
step is the reply's own `curl` line, parsed and run against the receiver. Only the session identity is
synthetic, as in `test_agent_upload_handoff.py`; the receiver needs one, and a stdio session has none.
"""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import io
import os
import shlex
import shutil
import tarfile
import types
import zipfile
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from fastmcp import Client, FastMCP
from test_adoption_paging import PDF

from exomem import (
    adoption_run,
    archive_members,
    auth_sessions,
    cloud_cell,
    command_surface,
    held_uploads,
    import_stage,
    import_upload,
    server,
    server_auth,
    server_transfer,
    upload_handoff,
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
    yield _Agent(vault, receiver.http_app(), who, tmp_path)
    writer_lease.reset_managers_for_tests()


class _Agent:
    """What an agent does: ask for an upload, send bytes to the URL it was given, call `manage_memory`."""

    def __init__(self, vault: Path, receiver, who: principal.RequestPrincipal, scratch: Path) -> None:
        self.vault = vault
        self.receiver = receiver
        self.who = who
        self.scratch = scratch
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

    async def send(self, step: dict, archive: bytes) -> httpx.Response:
        """Run the reply's `send` line: its headers, form fields and URL, with `<archive>` replaced by a real file."""
        path = self.scratch / "archive.bin"
        path.write_bytes(archive)
        words = iter(shlex.split(step["next"][0]["shell"].replace("<archive>", str(path))))
        assert next(words) == "curl"
        headers: dict[str, str] = {}
        fields: dict[str, str] = {}
        files: dict[str, tuple[str, bytes, str]] = {}
        url = ""
        for word in words:
            if word == "-H":
                name, _, value = next(words).partition(": ")
                headers[name] = value
            elif word == "-F":
                name, _, value = next(words).partition("=")
                if value.startswith("@"):
                    files[name] = (path.name, Path(value[1:]).read_bytes(), "application/octet-stream")
                else:
                    fields[name] = value
            elif word != "-sS":
                url = word
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.receiver)) as http:
            return await http.post(url, headers=headers, data=fields, files=files)

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


def _folders(files: dict[str, bytes]) -> list[str]:
    """Every folder above a member, shallowest first, as `tar` and `zip -r` list them."""
    return sorted({"/".join(name.split("/")[:depth]) for name in files for depth in range(1, name.count("/") + 1)})


def _tar(
    files: dict[str, bytes], *, extra: tuple[tarfile.TarInfo, ...] = (), mode: str = "w:gz", folders: bool = False,
) -> bytes:
    """A tar; gzip unless `mode` says otherwise, with an entry for each folder when `folders` says so."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode=mode) as archive:
        for folder in _folders(files) if folders else ():
            info = tarfile.TarInfo(folder + "/")
            info.type, info.mode = tarfile.DIRTYPE, 0o755
            archive.addfile(info)
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        for info in extra:
            archive.addfile(info)
    return buffer.getvalue()


def _zip(files: dict[str, bytes], *, method: int = zipfile.ZIP_DEFLATED, folders: bool = False) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", method) as archive:
        for folder in _folders(files) if folders else ():
            archive.writestr(folder + "/", b"")
        for name, data in files.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def _link(name: str, kind: bytes, target: str) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type, info.linkname = kind, target
    return info


def _with_comment(size: int) -> tarfile.TarInfo:
    """An empty file whose pax header carries `size` bytes of comment, which compress to a few bytes."""
    info = tarfile.TarInfo("commented.txt")
    info.pax_headers = {"comment": "x" * size}
    return info


def _pax_record(key: str, value: str) -> bytes:
    """One `length key=value` record of a pax header, whose length counts its own digits."""
    body = f" {key}={value}\n".encode()
    length = len(body) + len(str(len(body)))
    length = len(body) + len(str(length))
    return b"%d" % length + body


def _entry(name: str, kind: bytes, data: bytes, pax: dict[str, str] | None = None) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type, info.size, info.pax_headers = kind, len(data), pax or {}
    return info


def _sparse_map_archive(pairs: int) -> bytes:
    """A gzip tar whose one entry is a GNU sparse 1.0 file: `tarfile` reads its map, which lists `pairs` pairs, from the data."""
    body = b"%d\n" % pairs + b"0\n" * (2 * pairs)
    sparse = {"GNU.sparse.major": "1", "GNU.sparse.minor": "0", "GNU.sparse.name": "sparse.bin", "GNU.sparse.realsize": "1"}
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz", format=tarfile.PAX_FORMAT) as archive:
        archive.addfile(_entry("sparse.bin", tarfile.REGTYPE, body, sparse), io.BytesIO(body))
    return buffer.getvalue()


def _chained_pax_archive(headers: int, size: int) -> bytes:
    """A gzip tar whose one file follows `headers` extended headers in a row, each a record of `size` bytes."""
    record = _pax_record("comment", "x" * size)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz", format=tarfile.USTAR_FORMAT) as archive:
        for n in range(headers):
            archive.addfile(_entry(f"header{n}", tarfile.XHDTYPE, record), io.BytesIO(record))
        archive.addfile(_entry("ok.pdf", tarfile.REGTYPE, PDF), io.BytesIO(PDF))
    return buffer.getvalue()


#: Sixteen components of 255 bytes: each is a legal name and the whole is under 4,096 bytes, but no stage path can hold it.
_LONG = "/".join("a" * 255 for _ in range(16))


@pytest.mark.parametrize("pack", [_tar, _zip], ids=["tar", "zip"])
def test_an_agent_imports_a_folder_by_sending_one_archive_and_repeating_one_call_until_done(
        agent, duplicate_index, pack):
    """Catches a route that cannot reach `done` from the reply alone, or that files a repeated file twice."""
    # The archive the guide teaches, `tar -czf folder.tgz folder`: the folder under its own name, with an entry for
    # each folder. Seven files, five byte strings: b.pdf repeats a.pdf in another folder, e.pdf repeats d.pdf.
    folder = {"folder/a.pdf": PDF + b"a", "folder/docs/b.pdf": PDF + b"a", "folder/docs/c.pdf": PDF + b"c",
              "folder/docs/deep/d.pdf": PDF + b"d", "folder/e.pdf": PDF + b"d", "folder/f.pdf": PDF + b"f",
              "folder/g.pdf": PDF + b"g"}
    unique = {hashlib.sha256(data).hexdigest() for data in folder.values()}
    before = agent.sources()
    archive = pack(folder, folders=True)

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
    ("tar bzip2", lambda: _tar({"ok.pdf": PDF}, mode="w:bz2"), "ARCHIVE_UNREADABLE", None),
    ("tar xz", lambda: _tar({"ok.pdf": PDF}, mode="w:xz"), "ARCHIVE_UNREADABLE", None),
    ("zip bzip2 member", lambda: _zip({"ok.pdf": PDF}, method=zipfile.ZIP_BZIP2), "ARCHIVE_MEMBER_REFUSED", "ok.pdf"),
    ("tar case alias", lambda: _tar({"Doc/a.pdf": PDF, "doc/b.pdf": PDF}), "ARCHIVE_MEMBER_REFUSED", "doc/b.pdf"),
    ("tar header over its limit", lambda: _tar({"ok.pdf": PDF}, extra=(_with_comment(2 * 1024**2),)), "ARCHIVE_TOO_LARGE", None),
    ("tar headers inflate past the limit", lambda: _tar({"ok.pdf": PDF}, extra=(_with_comment(100_000),)), "ARCHIVE_TOO_LARGE", None),
    ("tar name too long for the volume", lambda: _tar({_LONG: PDF}), "ARCHIVE_MEMBER_REFUSED", None),
    ("zip name too long for the volume", lambda: _zip({_LONG: PDF}), "ARCHIVE_MEMBER_REFUSED", None),
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



def _journey_to_done(agent, archive: bytes) -> list[dict]:
    async def journey():
        async with agent.mcp() as client:
            step = await agent.ask(client, archive)
            assert (await agent.send(step, archive)).status_code == 201
            return await agent.file_to_done(client, step["token"])

    return asyncio.run(journey())


def test_a_plain_tar_that_holds_a_zip_imports_every_member(agent, duplicate_index):
    """Catches an archive read as the zip near its end: its other files would be dropped and the import still say done."""
    inner = _zip({"inner.pdf": PDF + b"inner"})
    archive = _tar({"a.pdf": PDF + b"a", "docs/b.pdf": PDF + b"b", "bundle.zip": inner}, mode="w")

    done = _journey_to_done(agent, archive)[-1]

    assert done["filing"] == "done" and done["progress"]["failed"] == 0
    assert done["progress"]["filed"] == 3, "the zip is one more file of the folder, not the folder"


def test_a_gzip_stream_that_runs_on_after_the_end_block_is_not_read_to_its_end(agent, duplicate_index, monkeypatch):
    """Catches an unpack that reads past the end block: a few kilobytes of padding would cost minutes of the server."""
    monkeypatch.setenv("EXOMEM_IMPORT_MAX_EXPANDED_BYTES", "100000")
    monkeypatch.setattr(archive_members, "MAX_ENTRIES", 3)
    padded = gzip.compress(_tar({"a.pdf": PDF}, mode="w") + bytes(32 * 1024**2))

    done = _journey_to_done(agent, padded)[-1]

    assert done["filing"] == "done" and done["progress"]["filed"] == 1


@pytest.mark.parametrize("archive", [
    pytest.param(lambda: _sparse_map_archive(300_000), id="a sparse map of 1.2 MB"),
    pytest.param(lambda: _chained_pax_archive(headers=3, size=400_000), id="three extended headers of 400 KB each"),
])
def test_headers_that_are_each_under_the_limit_but_pass_it_together_are_refused(agent, archive):
    """Catches a header chain read whole: `tarfile` reads a sparse map and each chained header without a total bound."""
    body = archive()

    async def journey():
        async with agent.mcp() as client:
            step = await agent.ask(client, body)
            assert (await agent.send(step, body)).status_code == 201
            return await agent.file(client, upload=step["token"])

    refused = asyncio.run(journey())["refused"]

    assert "ARCHIVE_TOO_LARGE" in refused and "headers hold more than" in refused
    assert agent.imports() == []


def test_a_gzip_tar_split_into_members_imports_every_file(agent, duplicate_index):
    """Catches a gzip file of several members read as a truncated tar, as bgzip and `cat a.gz b.gz` write it."""
    tar = _tar({"a.pdf": PDF + b"a", "docs/b.pdf": PDF + b"b", "docs/c.pdf": PDF + b"c"}, mode="w")
    middle = len(tar.rstrip(b"\0")) // 2  # inside the files, not in the padding that follows them
    archive = gzip.compress(tar[:middle]) + gzip.compress(tar[middle:]) + bytes(100)  # zeros after the last member

    done = _journey_to_done(agent, archive)[-1]

    assert done["filing"] == "done" and done["progress"]["filed"] == 3


@pytest.mark.parametrize("pack", [_tar, _zip], ids=["tar", "zip"])
def test_the_import_flushes_what_it_wrote_and_not_the_whole_host(agent, duplicate_index, monkeypatch, pack):
    """Catches a global `os.sync` on the route: it waits for every dirty page of the host inside one request."""
    def whole_host():
        raise AssertionError("the import called os.sync")

    monkeypatch.setattr(os, "sync", whole_host)

    done = _journey_to_done(agent, pack({f"doc{n}.pdf": PDF + bytes([n]) for n in range(3)}))[-1]

    assert done["filing"] == "done" and done["progress"]["filed"] == 3


def test_the_import_lane_is_refused_on_exomem_cloud(agent):
    """Catches a Cloud cell that hands out an import ticket: no Cloud route redeems it, and its guide is wrong there."""
    cloud = replace(agent.who, issuer_family=cloud_cell.issuer_family("cell-1"))

    with principal.request_scope(cloud), pytest.raises(ValueError) as refused:
        upload_handoff.prepare_upload(agent.vault, lane="import", expected_sha256="0" * 64)

    assert "UPLOAD_LANE_UNAVAILABLE" in str(refused.value) and "exomem cloud import" in str(refused.value)


@pytest.mark.parametrize("pack", [_tar, _zip], ids=["tar", "zip"])
def test_the_import_runs_on_a_platform_without_statvfs_or_sync(agent, duplicate_index, monkeypatch, pack):
    """Catches a Unix-only call on the route, which would fail every import on a Windows host."""
    monkeypatch.delattr(os, "statvfs")
    monkeypatch.delattr(os, "sync")
    # Windows answers `shutil.disk_usage` itself; the POSIX version needs `os.statvfs`.
    monkeypatch.setattr(shutil, "disk_usage", lambda path: types.SimpleNamespace(total=1 << 40, used=0, free=1 << 40))

    done = _journey_to_done(agent, pack({f"doc{n}.pdf": PDF + bytes([n]) for n in range(3)}))[-1]

    assert done["filing"] == "done" and done["progress"]["filed"] == 3


def test_a_stop_for_space_says_the_archive_was_not_kept_when_its_hold_ran_out(agent, monkeypatch):
    """Catches a reply that tells the agent to repeat a call whose archive is gone."""
    archive = _tar({"a.pdf": PDF})
    monkeypatch.setenv("EXOMEM_IMPORT_RESERVE_BYTES", str(1 << 60))
    skew = {"seconds": 0}
    now = held_uploads._now
    monkeypatch.setattr(held_uploads, "_now", lambda: now() + skew["seconds"])
    unpack = import_upload._unpack

    def outlasting(*args, **kwargs):
        try:
            unpack(*args, **kwargs)
        finally:
            skew["seconds"] = 2 * 3600  # the unpack outlasted the hold

    monkeypatch.setattr(import_upload, "_unpack", outlasting)

    async def journey():
        async with agent.mcp() as client:
            step = await agent.ask(client, archive)
            assert (await agent.send(step, archive)).status_code == 201
            return await agent.file(client, upload=step["token"])

    stopped = asyncio.run(journey())

    assert stopped["filing"] == "stopped"
    assert "was not kept" in stopped["person"][0]["text"] and "still held" not in stopped["person"][0]["text"]


def test_a_stage_that_no_record_vouches_for_tells_the_agent_its_copies_are_kept(agent, duplicate_index, monkeypatch):
    """Catches a done import that leaves staged files behind without saying so."""
    monkeypatch.setattr(import_stage.state_migration, "migration_status", lambda vault_root: "pending")

    replies = _journey_to_done(agent, _tar({"a.pdf": PDF + b"a", "b.pdf": PDF + b"b"}))

    done = replies[-1]
    assert [item["need"] for item in done["person"]] == ["discard"] and done["path"] in done["person"][0]["text"]
    assert sorted(path.name for path in (agent.vault / done["path"]).iterdir()) == ["a.pdf", "b.pdf"]
