"""Contract tests for direct preservation of client-owned file handles."""

from __future__ import annotations

import asyncio
import http.client
import json
import shutil
import socket
import sqlite3
import tempfile
import threading
import time
from contextlib import nullcontext
from pathlib import Path, PurePosixPath

import pytest
from conftest import initialize_vault_state_offline

from exomem import commands
from exomem import media_processing as media_processing_module


class _Response:
    def __init__(
        self, *, content_length: str, blocks: list[bytes], content_type: str = "image/png"
    ) -> None:
        self.status = 200
        self._content_length = content_length
        self._blocks = iter(blocks)
        self._content_type = content_type

    def getheader(self, name: str):
        return {"Content-Length": self._content_length, "Content-Type": self._content_type}.get(name)

    def read(self, _size: int) -> bytes:
        return next(self._blocks)

    def close(self) -> None:
        pass


class _Connection:
    def __init__(self, response: _Response) -> None:
        self.response = response
        self.sock = type("Socket", (), {"timeouts": [], "settimeout": lambda self, value: self.timeouts.append(value)})()

    def putrequest(self, *_args, **_kwargs) -> None:
        pass

    def putheader(self, *_args, **_kwargs) -> None:
        pass

    def endheaders(self) -> None:
        pass

    def getresponse(self) -> _Response:
        return self.response

    def close(self) -> None:
        pass


def _command(name: str):
    return next(command for command in commands.PRODUCT_COMMANDS if command.name == name)


def test_preserve_artifacts_has_openai_file_parameter_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import server

    command = _command("preserve_artifacts")

    assert command.mcp_meta == {"openai/fileParams": ("files",)}
    assert command.cli_writes is True
    assert {param.name for param in command.params} == {
        "scope",
        "category",
        "files",
        "adoption",
        "response_detail",
    }
    vault_root = tmp_path / "vault"
    shutil.copytree(Path(__file__).resolve().parent / "fixtures", vault_root)
    initialize_vault_state_offline(vault_root, source="client artifact MCP fixture")
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault_root))
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "lease"))
    monkeypatch.setenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", "1")
    tool = next(
        tool
        for tool in asyncio.run(server.build_server(require_auth=False).list_tools())
        if tool.name == "preserve_artifacts"
    ).to_mcp_tool().model_dump(mode="json", by_alias=True)

    files = tool["inputSchema"]["properties"]["files"]
    assert tool["_meta"]["openai/fileParams"] == ["files"]
    assert files["minItems"] == 1
    assert files["maxItems"] == 8
    assert list(files["items"]["properties"]) == [
        "download_url",
        "file_id",
        "mime_type",
        "file_name",
    ]
    assert files["items"]["required"] == ["download_url", "file_id"]
    assert files["items"]["properties"]["file_id"]["minLength"] == 1
    assert files["items"]["properties"]["file_id"]["maxLength"] == 256
    assert files["items"]["properties"]["mime_type"]["maxLength"] == 255


@pytest.mark.parametrize(
    "files",
    [
        json.dumps([{"download_url": "https://files.example/?signature=secret", "file_id": "   "}]),
        json.dumps(
            [
                {
                    "download_url": "https://files.example/?signature=secret",
                    "file_id": "file-one",
                    "mime_type": "x" * 256,
                }
            ]
        ),
    ],
)
def test_cli_file_handle_validation_is_bounded_and_content_free(files: str) -> None:
    from exomem import cli_ops

    with pytest.raises(cli_ops.OpError) as error:
        cli_ops.coerce(
            _command("preserve_artifacts").params,
            {"files": files},
            tool="preserve_artifacts",
            cli=True,
        )

    assert error.value.code == "INVALID_FILE"
    assert "signature" not in error.value.message


def test_compact_terminal_keeps_bounded_failure_for_malformed_artifact_rows() -> None:
    from exomem.mutation_terminal import committed_terminal, project_terminal

    terminal = committed_terminal(
        {
            "files": [
                {
                    "file_id": "file-one",
                    "outcome": "stored",
                    "stored_path": "Knowledge Base/Evidence/case/raw/proof.bin",
                    "size": True,
                    "hash": "a" * 64,
                    "hash_algorithm": "sha256",
                    "content_type": "application/octet-stream",
                    "media_id": None,
                    "warnings": ["x" * 301],
                }
            ],
            "summary": {"stored": True, "failed": 0},
        },
        request_id="request",
        receipt_id="receipt",
        idempotency_key=None,
    )

    compact = project_terminal(terminal)
    assert compact["files"] == [
        {
            "file_id": "file-one",
            "outcome": "failed",
            "code": "INVALID_ARTIFACT_RECEIPT",
            "reason": "artifact result was invalid",
        }
    ]
    assert compact["summary"] == {"stored": 0, "failed": 1}


def test_client_artifact_url_validation_never_returns_the_signed_url() -> None:
    from exomem.client_artifacts import SafeFetchError, validate_download_url

    with pytest.raises(SafeFetchError) as error:
        validate_download_url("http://127.0.0.1/private?token=do-not-log")

    assert error.value.code == "SAFE_FETCH_FAILED"
    assert "token" not in error.value.reason
    assert "127.0.0.1" not in error.value.reason


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.8", "::1", "fe80::1"])
def test_client_artifact_rejects_nonpublic_resolved_addresses(address: str) -> None:
    from exomem.client_artifacts import SafeFetchError, resolve_public_addresses

    with pytest.raises(SafeFetchError, match="destination is not public"):
        resolve_public_addresses("files.example", 443, resolver=lambda *_args: [address])


def test_client_artifact_redirects_are_validated_again() -> None:
    from exomem.client_artifacts import SafeFetchError, validate_redirect_url

    with pytest.raises(SafeFetchError) as error:
        validate_redirect_url("https://files.example/ok", "http://127.0.0.1/private?token=no")

    assert error.value.reason == "download URL must use HTTPS"


def test_client_artifact_budget_enforces_file_and_aggregate_caps() -> None:
    from exomem.client_artifacts import FetchBudget, SafeFetchError

    budget = FetchBudget(max_file_bytes=3, max_total_bytes=4)
    budget.consume(3)
    with pytest.raises(SafeFetchError, match="size limit"):
        budget.consume(2)
    with pytest.raises(SafeFetchError, match="size limit"):
        FetchBudget(max_file_bytes=3, max_total_bytes=10).validate_content_length(4)


@pytest.mark.parametrize(
    ("content_length", "blocks"),
    [("3", [b"ab", b"cd", b""]), ("5", [b"abc", b""])],
)
def test_staging_rejects_mismatched_content_length_and_removes_temp_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    content_length: str,
    blocks: list[bytes],
) -> None:
    from exomem import client_artifacts

    response = _Response(content_length=content_length, blocks=blocks)
    original_mkstemp = tempfile.mkstemp
    monkeypatch.setattr(
        client_artifacts, "resolve_public_addresses", lambda *_args, **_kwargs: ("8.8.8.8",)
    )
    monkeypatch.setattr(
        client_artifacts,
        "_PinnedHTTPSConnection",
        lambda *_args, **_kwargs: _Connection(response),
    )
    monkeypatch.setattr(
        client_artifacts.tempfile,
        "mkstemp",
        lambda **_kwargs: original_mkstemp(dir=tmp_path),
    )

    with pytest.raises(client_artifacts.SafeFetchError, match="Content-Length"):
        client_artifacts.stage_artifact(
            {"download_url": "https://files.example/proof", "file_id": "file-one"},
            client_artifacts.FetchBudget(),
        )

    assert list(tmp_path.iterdir()) == []


def test_retrieval_deadline_bounds_every_attempt() -> None:
    from exomem.client_artifacts import SafeFetchError, remaining_retrieval_timeout

    assert remaining_retrieval_timeout(12.0, clock=lambda: 10.0) == 2.0
    with pytest.raises(SafeFetchError, match="timed out"):
        remaining_retrieval_timeout(10.0, clock=lambda: 10.0)


# A deadlock valve, not a latency claim. Both tests below hand the code a
# 0.01s deadline and then block the underlying call indefinitely. If the
# deadline never fires, the call never returns, the `finally` that releases the
# block never runs, and the test hangs -- so any bound at all catches the bug
# and a generous one catches it just as well. The previous 0.5s said something
# additional and untrue: that a contended Windows shard schedules a thread
# within half a second.
_DEADLINE_VALVE_SECONDS = 30.0


def test_bounded_retrieval_call_stops_blocked_headers_or_body() -> None:
    from exomem.client_artifacts import SafeFetchError, _bounded_retrieval_call

    release = threading.Event()
    started = time.monotonic()
    try:
        with pytest.raises(SafeFetchError, match="timed out"):
            _bounded_retrieval_call(release.wait, deadline=started + 0.01)
    finally:
        release.set()

    assert time.monotonic() - started < _DEADLINE_VALVE_SECONDS


# How long the peer waits before closing the socket, and how long the test
# will accept the cancel taking. The gap between them is the whole
# discriminating power of the assertion, so the hold stays far larger than
# the observation.
_PEER_CLOSE_SECONDS = 30.0
_CANCEL_OBSERVE_SECONDS = 10.0


def test_staging_cancels_real_http_response_before_close_on_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import client_artifacts

    class Connection:
        def __init__(self, client: socket.socket) -> None:
            self.sock = client

        def putrequest(self, *_args, **_kwargs) -> None:
            pass

        def putheader(self, *_args, **_kwargs) -> None:
            pass

        def endheaders(self) -> None:
            pass

        def getresponse(self) -> http.client.HTTPResponse:
            response = http.client.HTTPResponse(self.sock)
            response.begin()
            return response

        def close(self) -> None:
            self.sock.close()

    client, peer = socket.socketpair()
    peer.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 1\r\n\r\n")
    connection = Connection(client)
    # The peer close is the WRONG way for this read to end, and it exists
    # only so a broken cancel cannot hang the suite. Keeping it far away
    # from the deadline is what makes the assertion below discriminating:
    # the read must end because its own 0.01s deadline fired, not because
    # the socket went away underneath it.
    release = threading.Timer(_PEER_CLOSE_SECONDS, peer.close)
    monkeypatch.setattr(client_artifacts, "_RETRIEVAL_DEADLINE_SECONDS", 0.01)
    monkeypatch.setattr(client_artifacts, "resolve_public_addresses", lambda *_args, **_kwargs: ("8.8.8.8",))
    monkeypatch.setattr(client_artifacts, "_PinnedHTTPSConnection", lambda *_args, **_kwargs: connection)
    original_mkstemp = tempfile.mkstemp
    monkeypatch.setattr(
        client_artifacts.tempfile,
        "mkstemp",
        lambda **_kwargs: original_mkstemp(prefix="exomem-artifact-"),
    )

    started = time.monotonic()
    release.start()
    try:
        with pytest.raises(client_artifacts.SafeFetchError, match="timed out"):
            client_artifacts.stage_artifact(
                {"download_url": "https://files.example/proof", "file_id": "file-one"},
                client_artifacts.FetchBudget(),
            )
    finally:
        release.cancel()
        peer.close()

    # Not a latency assertion. 0.1s claimed the runner would schedule the
    # cancel within a tenth of a second of the deadline; a Windows CI shard
    # took 0.124s while cancelling perfectly correctly. What is under test
    # is WHICH of the two exits happened, and any value comfortably below
    # the peer close answers that with room for a contended machine.
    assert time.monotonic() - started < _CANCEL_OBSERVE_SECONDS


def test_staging_routes_headers_and_body_through_the_absolute_deadline_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import client_artifacts

    response = _Response(content_length="2", blocks=[b"ok", b""])
    connection = _Connection(response)
    guarded: list[object] = []
    monkeypatch.setattr(client_artifacts, "resolve_public_addresses", lambda *_args, **_kwargs: ("8.8.8.8",))
    monkeypatch.setattr(client_artifacts, "_PinnedHTTPSConnection", lambda *_args, **_kwargs: connection)
    monkeypatch.setattr(
        client_artifacts,
        "_bounded_retrieval_call",
        lambda operation, **_kwargs: guarded.append(operation) or operation(),
    )
    original_mkstemp = tempfile.mkstemp
    monkeypatch.setattr(client_artifacts.tempfile, "mkstemp", lambda **_kwargs: original_mkstemp(dir=tmp_path))

    staged = client_artifacts.stage_artifact(
        {"download_url": "https://files.example/proof", "file_id": "file-one"},
        client_artifacts.FetchBudget(),
    )
    staged.path.unlink()

    assert len(guarded) == 3  # getresponse plus each body read


def test_mixed_batch_keeps_truth_when_one_content_type_is_oversized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import client_artifacts
    from exomem.mutation_terminal import committed_terminal, project_terminal

    staged: list[client_artifacts.StagedArtifact] = []
    for file_id, content_type in (("file-good", "application/octet-stream"), ("file-bad", "x" * 256)):
        path = tmp_path / f"{file_id}.bin"
        path.write_bytes(b"ok")
        staged.append(
            client_artifacts.StagedArtifact(
                file_id=file_id,
                path=path,
                size=2,
                sha256="a" * 64,
                content_type=content_type,
                filename=f"{file_id}.bin",
            )
        )
    monkeypatch.setattr(
        client_artifacts, "stage_artifact", lambda *_args, **_kwargs: staged.pop(0)
    )
    monkeypatch.setattr(
        client_artifacts,
        "preserve_stream",
        lambda _vault, **kwargs: type(
            "Result",
            (),
            {
                "as_dict": lambda self: {
                    "path": f"Knowledge Base/Evidence/case/raw/{kwargs['filename']}",
                    "size": 2,
                    "hash": "a" * 64,
                    "hash_algorithm": "sha256",
                    "content_type": "application/octet-stream",
                    "warnings": [],
                }
            },
        )(),
    )
    monkeypatch.setattr(
        client_artifacts,
        "active_manager",
        lambda: type("Manager", (), {"mutation_guard": lambda self, *_args, **_kwargs: nullcontext()})(),
    )

    result = commands.op_preserve_artifacts(
        tmp_path,
        scope="case",
        category="raw",
        files=[
            {"download_url": "https://files.example/good", "file_id": "file-good"},
            {"download_url": "https://files.example/bad", "file_id": "file-bad"},
        ],
    )
    compact = project_terminal(
        committed_terminal(result, request_id="request", receipt_id="receipt", idempotency_key=None)
    )

    assert [item["outcome"] for item in result["files"]] == ["stored", "failed"]
    assert [item["file_id"] for item in compact["files"]] == ["file-good", "file-bad"]
    assert compact["summary"] == {"stored": 1, "already_stored": 0, "failed": 1}


def test_mixed_long_collision_reason_stays_in_compact_artifact_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import client_artifacts, preserve
    from exomem.mutation_terminal import committed_terminal, project_terminal

    class ExistingArtifactPath:
        def __init__(self, *parts: str) -> None:
            self.parts = parts

        def __truediv__(self, part: object) -> ExistingArtifactPath:
            return ExistingArtifactPath(*self.parts, str(part))

        def exists(self) -> bool:
            return True

        def glob(self, _pattern: str) -> list[ExistingArtifactPath]:
            return []

        def relative_to(self, _vault_root: Path) -> PurePosixPath:
            return PurePosixPath(*self.parts)

    scope = "scope-" + "s" * 120
    category = "category-" + "c" * 120
    filename = "artifact-" + "a" * 120 + ".bin"
    staged: list[client_artifacts.StagedArtifact] = []
    for file_id, staged_filename in (("file-good", "good.bin"), ("file-collision", filename)):
        path = tmp_path / f"{file_id}.bin"
        path.write_bytes(b"ok")
        staged.append(
            client_artifacts.StagedArtifact(
                file_id=file_id,
                path=path,
                size=2,
                sha256="a" * 64,
                content_type="application/octet-stream",
                filename=staged_filename,
            )
        )
    real_preserve_stream = preserve.preserve_stream
    monkeypatch.setattr(client_artifacts, "stage_artifact", lambda *_args, **_kwargs: staged.pop(0))
    monkeypatch.setattr(preserve, "kb_root", lambda _vault_root: ExistingArtifactPath("Knowledge Base"))
    monkeypatch.setattr(
        client_artifacts,
        "preserve_stream",
        lambda vault_root, **kwargs: type(
            "Result",
            (),
            {
                "as_dict": lambda self: {
                    "path": "Knowledge Base/Evidence/case/raw/good.bin",
                    "size": 2,
                    "hash": "a" * 64,
                    "hash_algorithm": "sha256",
                    "content_type": "application/octet-stream",
                    "warnings": [],
                }
            },
        )()
        if kwargs["filename"] == "good.bin"
        else real_preserve_stream(vault_root, **kwargs),
    )
    monkeypatch.setattr(
        client_artifacts,
        "active_manager",
        lambda: type("Manager", (), {"mutation_guard": lambda self, *_args, **_kwargs: nullcontext()})(),
    )

    result = commands.op_preserve_artifacts(
        tmp_path,
        scope=scope,
        category=category,
        files=[
            {"download_url": "https://files.example/good", "file_id": "file-good"},
            {"download_url": "https://files.example/collision", "file_id": "file-collision"},
        ],
    )
    compact = project_terminal(
        committed_terminal(result, request_id="request", receipt_id="receipt", idempotency_key=None)
    )

    assert [item["outcome"] for item in compact["files"]] == ["stored", "failed"]
    assert compact["summary"] == {"stored": 1, "already_stored": 0, "failed": 1}
    assert compact["files"][1] == {
        "file_id": "file-collision",
        "outcome": "failed",
        "state": "failed",
        "code": "ARTIFACT_EXISTS",
        "reason": "artifact already exists",
    }


def test_staging_bounds_blocked_dns_resolution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from exomem import client_artifacts

    release = threading.Event()
    monkeypatch.setattr(client_artifacts, "_RETRIEVAL_DEADLINE_SECONDS", 0.01)
    # Freeze the budget clock, or this test races itself.
    #
    # Three separate sites consume the same 10ms before the resolver's own wait
    # is reached: the loop-top check in `stage_artifact`, and the two
    # `remaining_retrieval_timeout` calls in `_bounded_resolve` -- with an idna
    # encode, a semaphore acquire and a thread start in between. Every one of
    # them raises "download retrieval timed out" when the budget is already
    # gone, and only the resolver's `Empty` branch says "could not be
    # resolved". On a loaded Windows runner setup routinely costs more than
    # 10ms, so which message wins is a coin flip -- observed on two separate
    # PRs, on two different shards.
    #
    # Freezing `_monotonic` makes the budget non-decaying for those checks, so
    # the resolver's wait is the only place it can expire. `result.get` still
    # waits 10ms of REAL time (Queue does not use this clock), and the test's
    # own `time.monotonic()` below is untouched, so the boundedness assertion
    # still measures real elapsed time.
    monkeypatch.setattr(client_artifacts, "_monotonic", lambda: 0.0)
    monkeypatch.setattr(
        client_artifacts.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: release.wait() or [(None, None, None, None, ("8.8.8.8", 443))],
    )
    started = time.monotonic()
    try:
        with pytest.raises(client_artifacts.SafeFetchError, match="could not be resolved"):
            client_artifacts.stage_artifact(
                {"download_url": "https://files.example/proof", "file_id": "file-one"},
                client_artifacts.FetchBudget(),
            )
    finally:
        release.set()

    assert time.monotonic() - started < _DEADLINE_VALVE_SECONDS


def test_staging_resets_socket_timeout_before_each_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import client_artifacts

    response = _Response(content_length="2", blocks=[b"ok", b""])
    connection = _Connection(response)
    monkeypatch.setattr(client_artifacts, "resolve_public_addresses", lambda *_args, **_kwargs: ("8.8.8.8",))
    monkeypatch.setattr(client_artifacts, "_PinnedHTTPSConnection", lambda *_args, **_kwargs: connection)
    original_mkstemp = tempfile.mkstemp
    monkeypatch.setattr(client_artifacts.tempfile, "mkstemp", lambda **_kwargs: original_mkstemp(dir=tmp_path))

    staged = client_artifacts.stage_artifact(
        {"download_url": "https://files.example/proof", "file_id": "file-one"},
        client_artifacts.FetchBudget(),
    )
    staged.path.unlink()

    assert len(connection.sock.timeouts) == 2
    assert all(0 < timeout <= client_artifacts._RETRIEVAL_DEADLINE_SECONDS for timeout in connection.sock.timeouts)


def test_preserve_artifacts_refuses_invalid_destination_before_any_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import client_artifacts
    from exomem.cli_ops import OpError

    monkeypatch.setattr(
        client_artifacts,
        "stage_artifact",
        lambda *_args: pytest.fail("invalid scope must not fetch any file"),
    )

    with pytest.raises(OpError) as refused:
        commands.op_preserve_artifacts(
            tmp_path,
            scope="..",
            category="raw",
            files=[{"download_url": "https://files.example/proof", "file_id": "file-one"}],
        )

    assert refused.value.code == "INVALID_PRESERVE"
    assert refused.value.details["field"] == "scope"
    assert "'.' or '..'" in refused.value.details["reason"]


def test_preserve_artifacts_rejects_over_eight_files_before_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import client_artifacts

    monkeypatch.setattr(
        client_artifacts,
        "stage_artifact",
        lambda *_args: pytest.fail("over-limit batch must not fetch any file"),
    )
    files = [
        {"download_url": "https://files.example/proof", "file_id": f"file-{index}"}
        for index in range(9)
    ]

    result = commands.op_preserve_artifacts(tmp_path, scope="case", category="raw", files=files)

    assert result["summary"] == {"stored": 0, "already_stored": 0, "failed": 9}
    assert {item["code"] for item in result["files"]} == {"TOO_MANY_FILES"}


def test_preserve_artifacts_uses_the_narrow_replay_boundary() -> None:
    from exomem import writer_lease

    assert "preserve_artifacts" in writer_lease._NARROW_BOUNDARY_COMMANDS


def test_eight_artifacts_keep_ordered_receipt_and_replay_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import client_artifacts, writer_lease

    (tmp_path / "Knowledge Base").mkdir()
    command = _command("preserve_artifacts")
    stage_calls: list[str] = []
    commit_calls: list[str] = []

    def stage(file, _budget, **_kwargs):
        file_id = file["file_id"]
        path = tmp_path / f"{file_id}.bin"
        path.write_bytes(file_id.encode())
        stage_calls.append(file_id)
        return client_artifacts.StagedArtifact(
            file_id=file_id,
            path=path,
            size=len(file_id),
            sha256="a" * 64,
            content_type="application/octet-stream",
            filename=f"{file_id}.bin",
        )

    monkeypatch.setattr(client_artifacts, "stage_artifact", stage)
    monkeypatch.setattr(
        client_artifacts,
        "preserve_stream",
        lambda _vault, **kwargs: commit_calls.append(kwargs["filename"])
        or type(
            "Result",
            (),
            {
                "as_dict": lambda self: {
                    "path": f"Knowledge Base/Evidence/case/raw/{kwargs['filename']}",
                    "size": 6,
                    "hash": "a" * 64,
                    "hash_algorithm": "sha256",
                    "content_type": "application/octet-stream",
                    "warnings": ["media warning"] if kwargs["filename"] == "file-0.bin" else [],
                }
            },
        )(),
    )
    monkeypatch.setattr(
        client_artifacts,
        "active_manager",
        lambda: type("Manager", (), {"mutation_guard": lambda self, *_args, **_kwargs: nullcontext()})(),
    )
    manager = writer_lease.LeaseManager(writer_lease.LeaseConfig(state_dir=tmp_path / "state"))
    monkeypatch.setattr(writer_lease, "get_manager", lambda: manager)
    files = [
        {"download_url": f"https://files.example/{index}", "file_id": f"file-{index}"}
        for index in range(8)
    ]

    first = writer_lease.invoke_command(
        command, tmp_path, scope="case", category="raw", files=files, idempotency_key="eight-files"
    )
    replay = writer_lease.invoke_command(
        command, tmp_path, scope="case", category="raw", files=files, idempotency_key="eight-files"
    )

    assert first == replay
    assert [row["file_id"] for row in first["files"]] == [file["file_id"] for file in files]
    assert first["summary"] == {"stored": 8, "already_stored": 0, "failed": 0}
    assert first["warnings_count"] == 1
    assert first["paths"] == [
        f"Knowledge Base/Evidence/case/raw/file-{index}.bin" for index in range(8)
    ]
    assert stage_calls == [file["file_id"] for file in files]
    assert commit_calls == [f"file-{index}.bin" for index in range(8)]
    assert not list(tmp_path.glob("file-*.bin"))


def test_unexpected_commit_failure_removes_every_staged_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import client_artifacts

    staged = []
    for index in range(2):
        path = tmp_path / f"staged-{index}.bin"
        path.write_bytes(b"ok")
        staged.append(
            client_artifacts.StagedArtifact(
                file_id=f"file-{index}",
                path=path,
                size=2,
                sha256=str(index) * 64,
                content_type="application/octet-stream",
                filename=f"proof-{index}.bin",
            )
        )
    monkeypatch.setattr(
        client_artifacts, "stage_artifact", lambda file, _budget, **_kwargs: staged.pop(0)
    )
    monkeypatch.setattr(
        client_artifacts,
        "preserve_stream",
        lambda *_args, **_kwargs: type(
            "BrokenResult", (), {"as_dict": lambda self: (_ for _ in ()).throw(RuntimeError("broken result"))}
        )(),
    )
    monkeypatch.setattr(
        client_artifacts,
        "active_manager",
        lambda: type("Manager", (), {"mutation_guard": lambda self, *_args, **_kwargs: nullcontext()})(),
    )

    with pytest.raises(RuntimeError, match="broken result"):
        commands.op_preserve_artifacts(
            tmp_path,
            scope="case",
            category="raw",
            files=[
                {"download_url": "https://files.example/one", "file_id": "file-0"},
                {"download_url": "https://files.example/two", "file_id": "file-1"},
            ],
        )

    assert not list(tmp_path.glob("staged-*.bin"))


def test_client_artifact_filename_fallback_is_deterministic() -> None:
    from exomem.client_artifacts import fallback_filename

    assert (
        fallback_filename("a" * 64, "image/png")
        == "attachment-aaaaaaaaaaaaaaaa.png"
    )


def test_preserve_artifacts_reports_each_file_and_marks_committed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import client_artifacts

    staged = client_artifacts.StagedArtifact(
        file_id="file-ok",
        path=tmp_path / "staged.bin",
        size=2,
        sha256="b" * 64,
        content_type="application/octet-stream",
        filename="proof.bin",
    )
    staged.path.write_bytes(b"ok")
    def stage_artifact(file, _budget, **_kwargs):
        if file["file_id"] == "file-bad":
            raise client_artifacts.SafeFetchError("SAFE_FETCH_FAILED", "download URL must use HTTPS")
        return staged

    monkeypatch.setattr(client_artifacts, "stage_artifact", stage_artifact)

    calls: list[str] = []

    def preserve_stream(_vault, **kwargs):
        calls.append(kwargs["filename"])
        return type(
            "Result",
            (),
            {
                "as_dict": lambda self: {
                    "stored_path": "Knowledge Base/Evidence/case/raw/proof.bin",
                    "size": 2,
                    "hash": "b" * 64,
                    "hash_algorithm": "sha256",
                    "media_id": "sha256:" + "b" * 64,
                    "content_type": "application/octet-stream",
                    "warnings": [],
                }
            },
        )()

    monkeypatch.setattr(client_artifacts, "preserve_stream", preserve_stream)
    monkeypatch.setattr(
        client_artifacts, "mark_active_mutation_committed", lambda: calls.append("commit")
    )
    monkeypatch.setattr(
        client_artifacts,
        "active_manager",
        lambda: type("Manager", (), {"mutation_guard": lambda self, *_args, **_kwargs: nullcontext()})(),
    )

    result = commands.op_preserve_artifacts(
        tmp_path,
        scope="case",
        category="raw",
        files=[
            {"download_url": "https://files.example/proof", "file_id": "file-ok"},
            {"download_url": "http://invalid.example/nope", "file_id": "file-bad"},
        ],
    )

    assert calls == ["proof.bin", "commit"]
    assert result["summary"] == {"stored": 1, "already_stored": 0, "failed": 1}
    assert [item["file_id"] for item in result["files"]] == ["file-ok", "file-bad"]
    assert result["files"][0]["outcome"] == "stored"
    assert result["files"][1] == {
        "file_id": "file-bad",
        "outcome": "failed",
        "state": "failed",
        "code": "SAFE_FETCH_FAILED",
        "reason": "download URL must use HTTPS",
    }


def test_preserve_artifacts_keeps_append_only_collision_as_one_failed_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import client_artifacts, preserve

    staged = client_artifacts.StagedArtifact(
        file_id="file-collision",
        path=tmp_path / "staged.bin",
        size=2,
        sha256="c" * 64,
        content_type="application/octet-stream",
        filename="proof.bin",
    )
    staged.path.write_bytes(b"ok")
    monkeypatch.setattr(client_artifacts, "stage_artifact", lambda *_args, **_kwargs: staged)
    monkeypatch.setattr(
        client_artifacts,
        "preserve_stream",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            preserve.PreserveError("ARTIFACT_EXISTS", ["filename"], "already exists")
        ),
    )
    monkeypatch.setattr(
        client_artifacts,
        "active_manager",
        lambda: type("Manager", (), {"mutation_guard": lambda self, *_args, **_kwargs: nullcontext()})(),
    )

    result = commands.op_preserve_artifacts(
        tmp_path,
        scope="case",
        category="raw",
        files=[{"download_url": "https://files.example/proof", "file_id": "file-collision"}],
    )

    assert result["summary"] == {"stored": 0, "already_stored": 0, "failed": 1}
    assert result["files"][0]["code"] == "ARTIFACT_EXISTS"


def test_preserve_artifacts_marks_commit_before_media_reconciliation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import client_artifacts, media_processing

    staged = client_artifacts.StagedArtifact(
        file_id="file-media",
        path=tmp_path / "staged.bin",
        size=2,
        sha256="d" * 64,
        content_type="image/png",
        filename="proof.png",
    )
    staged.path.write_bytes(b"ok")
    events: list[str] = []
    monkeypatch.setattr(client_artifacts, "stage_artifact", lambda *_args, **_kwargs: staged)
    monkeypatch.setattr(
        client_artifacts,
        "preserve_stream",
        lambda *_args, **_kwargs: type(
            "Result", (), {"as_dict": lambda self: {"path": "Knowledge Base/Evidence/case/raw/proof.png", "warnings": []}}
        )(),
    )
    monkeypatch.setattr(
        client_artifacts, "mark_active_mutation_committed", lambda: events.append("commit")
    )
    monkeypatch.setattr(
        client_artifacts,
        "active_manager",
        lambda: type("Manager", (), {"mutation_guard": lambda self, *_args, **_kwargs: nullcontext()})(),
    )

    def reconcile_media(*_args, **_kwargs) -> None:
        events.append("reconcile")
        raise RuntimeError("worker unavailable")

    monkeypatch.setattr(media_processing, "classify_media", lambda _path: "image")
    monkeypatch.setattr(media_processing, "reconcile_media", reconcile_media)

    result = commands.op_preserve_artifacts(
        tmp_path,
        scope="case",
        category="raw",
        files=[{"download_url": "https://files.example/proof", "file_id": "file-media"}],
    )

    assert events == ["commit", "reconcile"]
    assert result["files"][0]["warnings"] == [
        "media reconciliation failed; evidence remains recoverable"
    ]


class _NullBoundaryManager:
    def mutation_guard(self, *_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        return nullcontext()


def _staged(tmp_path: Path, file_id: str, filename: str, payload: bytes):  # noqa: ANN202
    """One freshly staged artifact; `preserve_artifacts` unlinks what it gets."""
    import hashlib
    import uuid

    from exomem import client_artifacts

    path = tmp_path / f"{file_id}-{uuid.uuid4().hex}.bin"
    path.write_bytes(payload)
    return client_artifacts.StagedArtifact(
        file_id=file_id,
        path=path,
        size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        content_type="application/octet-stream",
        filename=filename,
    )


def test_preserve_artifacts_reports_one_terminal_state_per_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every row carries a terminal `state` and the identity of what landed.

    `outcome` stays for one release and mirrors `state`, so an existing client
    branching on `stored`/`failed` keeps working while a new one can tell a
    fresh commit from a duplicate that was already there.
    """
    from exomem import client_artifacts
    from exomem.mutation_terminal import committed_terminal

    staged_rows = {
        "file-stored": ("stored.bin", b"first"),
        "file-duplicate": ("duplicate.bin", b"first"),
    }

    def stage_artifact(file, _budget, **_kwargs):  # noqa: ANN001, ANN202
        if file["file_id"] == "file-bad":
            raise client_artifacts.SafeFetchError(
                "SAFE_FETCH_FAILED", "download URL must use HTTPS"
            )
        filename, payload = staged_rows[file["file_id"]]
        return _staged(tmp_path, file["file_id"], filename, payload)

    def preserve_stream(_vault, **kwargs):  # noqa: ANN001, ANN202
        stored = f"Knowledge Base/Evidence/case/raw/{kwargs['filename']}"
        return type(
            "Result",
            (),
            {
                "as_dict": lambda self: {
                    "path": stored,
                    "stored_path": stored,
                    "sidecar_path": f"{stored}.md",
                    "ref": "exomem://note/abcdef0123456789",
                    "size": 5,
                    "hash": "c" * 64,
                    "hash_algorithm": "sha256",
                    "media_id": "sha256:" + "c" * 64,
                    "content_type": "application/octet-stream",
                    "warnings": [],
                }
            },
        )()

    monkeypatch.setattr(client_artifacts, "stage_artifact", stage_artifact)
    monkeypatch.setattr(client_artifacts, "preserve_stream", preserve_stream)
    monkeypatch.setattr(client_artifacts, "mark_active_mutation_committed", lambda: None)
    monkeypatch.setattr(client_artifacts, "active_manager", _NullBoundaryManager)

    result = commands.op_preserve_artifacts(
        tmp_path,
        scope="case",
        category="raw",
        files=[
            {"download_url": "https://files.example/one", "file_id": "file-stored"},
            {"download_url": "http://invalid.example/nope", "file_id": "file-bad"},
        ],
    )

    stored, failed = result["files"]
    assert stored["state"] == "stored"
    assert stored["outcome"] == "stored"
    assert stored["path"] == "Knowledge Base/Evidence/case/raw/stored.bin"
    assert stored["stored_path"] == stored["path"]
    assert stored["ref"] == "exomem://note/abcdef0123456789"
    assert stored["hash"] == "c" * 64
    assert stored["hash_algorithm"] == "sha256"
    assert stored["size"] == 5
    assert stored["media_id"] == "sha256:" + "c" * 64
    assert stored["content_type"] == "application/octet-stream"
    assert stored["warnings"] == []
    assert failed["state"] == "failed"
    assert failed["outcome"] == "failed"
    assert result["summary"] == {"stored": 1, "already_stored": 0, "failed": 1}

    terminal = committed_terminal(
        result, request_id="request-1", receipt_id="0123456789abcdef", idempotency_key=None
    )
    assert terminal["request_id"] == "request-1"
    assert terminal["receipt_id"] == "0123456789abcdef"


@pytest.mark.parametrize(
    ("scope", "category", "field", "needle"),
    (
        ("food/caviarhouse-group-order", "roe-tasting", "scope", "/"),
        ("food", "roe:tasting", "category", ":"),
        ("food", "roe\x01tasting", "category", "control character"),
        ("  food  ", "roe-tasting", "scope", "whitespace"),
        ("food", "roe-tasting ", "category", "whitespace"),
    ),
)
def test_destination_segments_are_refused_never_normalised(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
    scope: str,
    category: str,
    field: str,
    needle: str,
) -> None:
    """A malformed destination is refused before a single byte moves.

    The live vault holds one logical case split across
    `Evidence/food/caviarhouse-group-order/` and
    `Evidence/foodcaviarhouse-group-order/` because the separator was deleted
    instead of refused. Silent normalisation forks the tree; a named refusal
    costs the caller one corrected call.
    """
    from exomem import client_artifacts
    from exomem.cli_ops import OpError

    monkeypatch.setattr(
        client_artifacts,
        "stage_artifact",
        lambda *_args, **_kwargs: pytest.fail("a malformed destination must not fetch"),
    )

    with pytest.raises(OpError) as refused:
        client_artifacts.preserve_artifacts(
            vault,
            scope=scope,
            category=category,
            files=[{"download_url": "https://files.example/a", "file_id": "file-one"}],
        )

    assert refused.value.code == "INVALID_PRESERVE"
    assert refused.value.details["field"] == field
    assert needle in refused.value.details["reason"]
    assert "one path segment" in refused.value.details["accepted_form"]
    evidence = vault / "Knowledge Base" / "Evidence"
    assert not (evidence / "foodcaviarhouse-group-order").exists()
    assert not (evidence / "food").exists()


def test_preserve_evidence_refuses_a_reserved_character_in_category(vault: Path) -> None:
    """The text lane refuses the same way, naming the field it refused."""
    from exomem.cli_ops import OpError

    with pytest.raises(OpError) as refused:
        commands.op_preserve_evidence(
            vault,
            scope="case",
            category="letters:2026",
            filename="letter.txt",
            content="Dear Mr. Kivi,",
        )

    assert refused.value.code == "INVALID_PRESERVE"
    assert refused.value.details["field"] == "category"
    assert not (vault / "Knowledge Base" / "Evidence" / "case").exists()


def test_valid_destination_segments_are_used_byte_for_byte(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import client_artifacts

    monkeypatch.setattr(
        client_artifacts,
        "stage_artifact",
        lambda file, _budget, **_kwargs: _staged(
            tmp_path, file["file_id"], "clean.bin", b"clean"
        ),
    )
    monkeypatch.setattr(client_artifacts, "mark_active_mutation_committed", lambda: None)
    monkeypatch.setattr(client_artifacts, "active_manager", _NullBoundaryManager)

    result = client_artifacts.preserve_artifacts(
        vault,
        scope="caviarhouse-group-order",
        category="roe-tasting-and-butter-mayo",
        files=[{"download_url": "https://files.example/a", "file_id": "file-one"}],
    )

    assert result["files"][0]["path"] == (
        "Knowledge Base/Evidence/caviarhouse-group-order/"
        "roe-tasting-and-butter-mayo/clean.bin"
    )


def test_all_duplicate_batch_still_carries_the_mutation_terminal(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retrying under a new identity is an all-duplicate batch by construction.

    That is this change's own recovery path, so it has to come back with an
    envelope -- `request_id`, `receipt_id`, bounded rows -- and not as a bare
    leaf dict that never reached the terminal projector at all.
    """
    from exomem import client_artifacts, writer_lease

    payload = b"all-duplicate-bytes"
    plan = {"file-first": "one.bin", "file-retry": "two.bin"}

    monkeypatch.setattr(
        client_artifacts,
        "stage_artifact",
        lambda file, _budget, **_kwargs: _staged(
            tmp_path, file["file_id"], plan[file["file_id"]], payload
        ),
    )
    manager = writer_lease.LeaseManager(writer_lease.LeaseConfig(state_dir=tmp_path / "state"))
    monkeypatch.setattr(writer_lease, "get_manager", lambda: manager)
    command = _command("preserve_artifacts")

    stored = writer_lease.invoke_command(
        command,
        vault,
        scope="case",
        category="raw",
        files=[{"download_url": "https://files.example/a", "file_id": "file-first"}],
        idempotency_key="all-duplicate-first",
    )
    assert stored["files"][0]["state"] == "stored"

    duplicate = writer_lease.invoke_command(
        command,
        vault,
        scope="case",
        category="raw",
        files=[{"download_url": "https://files.example/b", "file_id": "file-retry"}],
        idempotency_key="all-duplicate-retry",
    )

    assert duplicate["ok"] is True
    assert duplicate["status"] == "committed"
    assert duplicate["terminal"] is True
    assert duplicate["request_id"]
    assert duplicate["receipt_id"]
    assert duplicate["files"][0]["state"] == "already_stored"
    assert duplicate["summary"] == {"stored": 0, "already_stored": 1, "failed": 0}
    assert not (vault / "Knowledge Base" / "Evidence" / "case" / "raw" / "two.bin").exists()


def test_duplicate_preserve_evidence_still_carries_the_mutation_terminal(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The single-file lane needs the same envelope as its stored branch."""
    from exomem import writer_lease

    manager = writer_lease.LeaseManager(writer_lease.LeaseConfig(state_dir=tmp_path / "state"))
    monkeypatch.setattr(writer_lease, "get_manager", lambda: manager)
    command = _command("preserve_evidence")
    content = "Dear Mr. Kivi, please cease and desist."

    stored = writer_lease.invoke_command(
        command,
        vault,
        scope="case",
        category="letters",
        filename="letter.txt",
        content=content,
        idempotency_key="evidence-first",
    )
    assert stored["status"] == "committed"

    duplicate = writer_lease.invoke_command(
        command,
        vault,
        scope="case",
        category="letters",
        filename="letter-copy.txt",
        content=content,
        idempotency_key="evidence-retry",
    )

    assert duplicate["ok"] is True
    assert duplicate["status"] == "committed"
    assert duplicate["terminal"] is True
    assert duplicate["request_id"]
    assert duplicate["receipt_id"]
    assert not (
        vault / "Knowledge Base" / "Evidence" / "case" / "letters" / "letter-copy.txt"
    ).exists()


def test_identical_files_inside_one_batch_are_one_fact(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The destination index is built once but stays live for the batch."""
    from exomem import client_artifacts

    payload = b"same-bytes-twice"
    plan = {"file-one": "one.bin", "file-two": "two.bin"}

    monkeypatch.setattr(
        client_artifacts,
        "stage_artifact",
        lambda file, _budget, **_kwargs: _staged(
            tmp_path, file["file_id"], plan[file["file_id"]], payload
        ),
    )
    monkeypatch.setattr(client_artifacts, "mark_active_mutation_committed", lambda: None)
    monkeypatch.setattr(client_artifacts, "active_manager", _NullBoundaryManager)

    result = client_artifacts.preserve_artifacts(
        vault,
        scope="case",
        category="intra",
        files=[
            {"download_url": "https://files.example/one", "file_id": "file-one"},
            {"download_url": "https://files.example/two", "file_id": "file-two"},
        ],
    )

    assert [row["state"] for row in result["files"]] == ["stored", "already_stored"]
    assert result["summary"] == {"stored": 1, "already_stored": 1, "failed": 0}
    evidence = vault / "Knowledge Base" / "Evidence" / "case" / "intra"
    assert (evidence / "one.bin").is_file()
    assert not (evidence / "two.bin").exists()


def test_the_destination_is_read_once_for_the_whole_batch(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per-file rescans made an eight-file batch eight passes over the category."""
    from exomem import client_artifacts, preserve

    scans: list[str] = []
    real_index = preserve.destination_duplicate_index

    def counted(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        scans.append("scan")
        return real_index(*args, **kwargs)

    monkeypatch.setattr(client_artifacts, "destination_duplicate_index", counted)
    monkeypatch.setattr(
        client_artifacts,
        "stage_artifact",
        lambda file, _budget, **_kwargs: _staged(
            tmp_path, file["file_id"], f"{file['file_id']}.bin", file["file_id"].encode()
        ),
    )
    monkeypatch.setattr(client_artifacts, "mark_active_mutation_committed", lambda: None)
    monkeypatch.setattr(client_artifacts, "active_manager", _NullBoundaryManager)

    client_artifacts.preserve_artifacts(
        vault,
        scope="case",
        category="scanned",
        files=[
            {"download_url": f"https://files.example/{index}", "file_id": f"file-{index}"}
            for index in range(4)
        ],
    )

    assert scans == ["scan"]


def test_a_duplicate_without_a_resolvable_ref_is_stored_not_failed(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An `already_stored` outcome names a path *and* a ref, or it is not one.

    Half an identity used to reach the client as `INVALID_ARTIFACT_RECEIPT` for
    a file that was neither written nor failed.
    """
    from exomem import client_artifacts, preserve

    payload = b"unreffable-bytes"
    plan = {"file-first": "one.bin", "file-retry": "two.bin"}

    monkeypatch.setattr(
        client_artifacts,
        "stage_artifact",
        lambda file, _budget, **_kwargs: _staged(
            tmp_path, file["file_id"], plan[file["file_id"]], payload
        ),
    )
    monkeypatch.setattr(client_artifacts, "mark_active_mutation_committed", lambda: None)
    monkeypatch.setattr(client_artifacts, "active_manager", _NullBoundaryManager)

    stored = client_artifacts.preserve_artifacts(
        vault,
        scope="case",
        category="unreffable",
        files=[{"download_url": "https://files.example/a", "file_id": "file-first"}],
    )
    assert stored["files"][0]["state"] == "stored"

    monkeypatch.setattr(preserve.memory_refs, "ref_from_markdown", lambda _source: None)

    retry = client_artifacts.preserve_artifacts(
        vault,
        scope="case",
        category="unreffable",
        files=[{"download_url": "https://files.example/b", "file_id": "file-retry"}],
    )

    assert retry["files"][0]["state"] == "stored"
    assert retry["summary"] == {"stored": 1, "already_stored": 0, "failed": 0}


def test_a_sidecar_pointing_outside_its_destination_is_not_a_duplicate(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The page is vault data and its `artifact_path` reaches the client."""
    from exomem import preserve

    stored = preserve.preserve(
        vault,
        scope="case",
        category="contained",
        filename="proof.txt",
        content="contained bytes",
    )
    sidecar = vault / stored.sidecar_path
    escaped = sidecar.read_text(encoding="utf-8").replace(
        f"artifact_path: {stored.path}",
        "artifact_path: Knowledge Base/Evidence/case/elsewhere/proof.txt",
    )
    sidecar.write_text(escaped, encoding="utf-8", newline="\n")

    index = preserve.destination_duplicate_index(vault, scope="case", category="contained")

    assert index == {}


@pytest.mark.parametrize(
    ("value", "needle"),
    (
        ("x" * 300, "longer than"),
        ("case.", "cannot end with"),
        ("CON", "reserved device name"),
        ("com1.txt", "reserved device name"),
        ("ca​se", "control character"),
        ("ca‮se", "control character"),
    ),
)
def test_hostile_destination_segments_are_refused(value: str, needle: str) -> None:
    from exomem.preserve import destination_segment_refusal

    reason = destination_segment_refusal(value, field="scope")

    assert reason is not None
    assert needle in reason


def test_an_empty_batch_still_refuses_a_malformed_destination(vault: Path) -> None:
    """`files=[]` used to report a successful batch of nothing under `a/b`."""
    from exomem.cli_ops import OpError
    from exomem.client_artifacts import preserve_artifacts

    with pytest.raises(OpError) as refused:
        preserve_artifacts(vault, scope="food/caviar", category="raw", files=[])

    assert refused.value.details["field"] == "scope"


def test_mixed_batch_states_are_exact_when_the_middle_file_fails(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One item's failure erases nothing, and the counts are per state."""
    from exomem import client_artifacts

    plan = {
        "file-one": ("one.bin", b"one"),
        "file-three": ("three.bin", b"three"),
    }

    def stage_artifact(file, _budget, **_kwargs):  # noqa: ANN001, ANN202
        if file["file_id"] == "file-two":
            raise client_artifacts.SafeFetchError(
                "SAFE_FETCH_FAILED", "download could not be retrieved"
            )
        filename, payload = plan[file["file_id"]]
        return _staged(tmp_path, file["file_id"], filename, payload)

    monkeypatch.setattr(client_artifacts, "stage_artifact", stage_artifact)
    monkeypatch.setattr(client_artifacts, "mark_active_mutation_committed", lambda: None)
    monkeypatch.setattr(client_artifacts, "active_manager", _NullBoundaryManager)

    result = client_artifacts.preserve_artifacts(
        vault,
        scope="case",
        category="mixed",
        files=[
            {"download_url": "https://files.example/one", "file_id": "file-one"},
            {"download_url": "https://files.example/two", "file_id": "file-two"},
            {"download_url": "https://files.example/three", "file_id": "file-three"},
        ],
    )

    assert [row["file_id"] for row in result["files"]] == [
        "file-one",
        "file-two",
        "file-three",
    ]
    assert [row["state"] for row in result["files"]] == ["stored", "failed", "stored"]
    assert result["summary"] == {"stored": 2, "already_stored": 0, "failed": 1}
    assert "stored_path" not in result["files"][1]
    evidence = vault / "Knowledge Base" / "Evidence" / "case" / "mixed"
    assert (evidence / "one.bin").is_file()
    assert (evidence / "three.bin").is_file()


def test_preserve_evidence_reports_a_duplicate_instead_of_a_second_copy(
    vault: Path,
) -> None:
    """The text lane reads the same three-state vocabulary as the batch lane."""
    first = commands.op_preserve_evidence(
        vault,
        scope="case",
        category="letters",
        filename="2026-09-10-letter.txt",
        content="Dear Mr. Kivi, please cease and desist.",
    )
    assert first["state"] == "stored"

    again = commands.op_preserve_evidence(
        vault,
        scope="case",
        category="letters",
        filename="2026-09-10-letter-copy.txt",
        content="Dear Mr. Kivi, please cease and desist.",
    )
    assert again["state"] == "already_stored"
    assert again["duplicate_of"] == {"path": first["path"], "ref": first["ref"]}
    assert again["path"] == first["path"]
    assert not (
        vault / "Knowledge Base" / "Evidence" / "case" / "letters" / "2026-09-10-letter-copy.txt"
    ).exists()


def test_duplicate_bytes_under_a_new_identity_are_reported_not_stored(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The natural recovery -- retry under a new identity -- stops duplicating.

    Evidence is append-only, so the second write of the same bytes under the
    same destination is not a correction, it is a second copy of one fact. The
    lookup is destination-scoped on purpose: the same bytes in another evidence
    family are two facts and both are stored.
    """
    from exomem import client_artifacts

    payload = b"caviarhouse-invoice-bytes"
    plan = {
        "file-first": ("invoice.bin", payload),
        "file-retry": ("invoice-retry.bin", payload),
    }
    commits: list[str] = []

    def stage_artifact(file, _budget, **_kwargs):  # noqa: ANN001, ANN202
        filename, content = plan[file["file_id"]]
        return _staged(tmp_path, file["file_id"], filename, content)

    monkeypatch.setattr(client_artifacts, "stage_artifact", stage_artifact)
    monkeypatch.setattr(
        client_artifacts, "mark_active_mutation_committed", lambda: commits.append("commit")
    )
    monkeypatch.setattr(client_artifacts, "active_manager", _NullBoundaryManager)

    first = client_artifacts.preserve_artifacts(
        vault,
        scope="food",
        category="caviar",
        files=[{"download_url": "https://files.example/a", "file_id": "file-first"}],
    )
    row = first["files"][0]
    assert row["state"] == "stored"
    stored_path = row["path"]
    stored_ref = row["ref"]
    assert commits == ["commit"]

    evidence = vault / "Knowledge Base" / "Evidence" / "food" / "caviar"
    before = sorted(item.name for item in evidence.iterdir())

    retry = client_artifacts.preserve_artifacts(
        vault,
        scope="food",
        category="caviar",
        files=[{"download_url": "https://files.example/b", "file_id": "file-retry"}],
    )
    duplicate = retry["files"][0]
    assert duplicate["state"] == "already_stored"
    assert duplicate["outcome"] == "stored"
    assert duplicate["duplicate_of"] == {"path": stored_path, "ref": stored_ref}
    assert duplicate["path"] == stored_path
    assert duplicate["ref"] == stored_ref
    assert retry["summary"] == {"stored": 0, "already_stored": 1, "failed": 0}
    assert sorted(item.name for item in evidence.iterdir()) == before
    # The retry writes nothing and yet marks the mutation committed: it has a
    # terminal answer about canonical state, which is what earns it a terminal
    # envelope. The directory listing above is what proves nothing was written.
    assert commits == ["commit", "commit"]

    other_family = client_artifacts.preserve_artifacts(
        vault,
        scope="food",
        category="butter-mayo",
        files=[{"download_url": "https://files.example/c", "file_id": "file-retry"}],
    )
    assert other_family["files"][0]["state"] == "stored"
    assert other_family["summary"] == {"stored": 1, "already_stored": 0, "failed": 0}
    assert commits == ["commit", "commit", "commit"]


# --------------------------------------------------------------------------- #
# Media fan-out leaves the critical section
# --------------------------------------------------------------------------- #


class _FanoutWatch:
    """Where each index/graph fan-out happened, relative to the two boundaries.

    Recording the position rather than the count is the whole point. The defect
    was never that the fan-out ran — it is that it ran while the preservation
    guard was held and before the caller had its receipt, so a client that
    timed out kept retrying a batch that had already committed.

    Only the media sidecar's own fan-out is judged here. The canonical artifact
    commit fans out too, from its own `preserve_artifacts_commit` boundary;
    that one belongs to `shorten-mutation-critical-section` and asserting on it
    would make this test fail for a reason it does not describe.
    """

    def __init__(self, sidecar_name: str) -> None:
        self.sidecar_name = sidecar_name
        self.media_guard_depth = 0
        self.terminal_persisted = False
        self.fanouts: list[dict[str, object]] = []

    def record(self, name: str, paths) -> None:  # noqa: ANN001
        names = [Path(str(path)).name for path in paths or ()]
        if self.sidecar_name not in names:
            return
        self.fanouts.append(
            {
                "name": name,
                "paths": names,
                "inside_media_guard": self.media_guard_depth > 0,
                "after_terminal": self.terminal_persisted,
            }
        )


def _watch_media_fanout(
    monkeypatch: pytest.MonkeyPatch, manager, sidecar_name: str  # noqa: ANN001
) -> _FanoutWatch:
    from contextlib import contextmanager

    from exomem import index_sync, writer_lease
    from exomem import vault as vault_module

    watch = _FanoutWatch(sidecar_name)
    real_guard = writer_lease.LeaseManager.mutation_guard

    @contextmanager
    def tracking_guard(self, vault_root, **kwargs):  # noqa: ANN001, ANN202
        media = kwargs.get("operation") == "preserve_artifacts_media"
        with real_guard(self, vault_root, **kwargs) as coordinator:
            if media:
                watch.media_guard_depth += 1
            try:
                yield coordinator
            finally:
                if media:
                    watch.media_guard_depth -= 1

    monkeypatch.setattr(writer_lease.LeaseManager, "mutation_guard", tracking_guard)

    real_upsert = index_sync.upsert_after_write

    def watched_upsert(vault_root, written_paths, **kwargs):  # noqa: ANN001, ANN202
        watch.record("index_sync.upsert_after_write", written_paths)
        return real_upsert(vault_root, written_paths, **kwargs)

    monkeypatch.setattr(index_sync, "upsert_after_write", watched_upsert)

    real_fanout = vault_module.post_commit_batch_fanout

    def watched_fanout(vault_root, replaced, *args, **kwargs):  # noqa: ANN001, ANN202
        watch.record("post_commit_batch_fanout", replaced)
        return real_fanout(vault_root, replaced, *args, **kwargs)

    monkeypatch.setattr(vault_module, "post_commit_batch_fanout", watched_fanout)
    monkeypatch.setattr(
        media_processing_module, "post_commit_batch_fanout", watched_fanout
    )

    def terminal_persisted() -> None:
        watch.terminal_persisted = True

    manager.idempotency.after_terminal_persisted = terminal_persisted
    return watch


def _png(tmp_path: Path, file_id: str, filename: str):  # noqa: ANN202
    """A staged artifact `classify_media` recognises as media."""
    return _staged(tmp_path, file_id, filename, b"\x89PNG\r\n\x1a\n" + file_id.encode())


_ADOPTION = {
    "key": "media-fanout-adoption",
    "trigger": "selected",
    "selected_file_id": "file-media",
}


@pytest.mark.parametrize("adoption", [None, _ADOPTION], ids=["batch", "adoption"])
def test_media_fanout_never_runs_inside_the_guard_or_before_the_terminal(
    vault: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    adoption,  # noqa: ANN001
) -> None:
    """Both preservation lanes owe the same invariant, with no derived path."""
    from exomem import client_artifacts, deferred_index, writer_lease

    monkeypatch.setenv("EXOMEM_FAST_DURABLE_ACK", "0")
    monkeypatch.setattr(
        client_artifacts,
        "stage_artifact",
        lambda file, _budget, **_kwargs: _png(tmp_path, file["file_id"], "proof.png"),
    )
    manager = writer_lease.LeaseManager(
        writer_lease.LeaseConfig(state_dir=tmp_path / "state")
    )
    monkeypatch.setattr(writer_lease, "get_manager", lambda: manager)
    watch = _watch_media_fanout(monkeypatch, manager, "proof.png.md")

    terminal = writer_lease.invoke_command(
        _command("preserve_artifacts"),
        vault,
        scope="case",
        category="raw",
        files=[{"download_url": "https://files.example/p", "file_id": "file-media"}],
        adoption=adoption,
        idempotency_key=f"media-fanout-{adoption is not None}",
    )

    assert terminal["status"] == "committed"
    sidecar_only = [row for row in watch.fanouts if row["paths"] == ["proof.png.md"]]
    assert sidecar_only, "the media sidecar produced no index or graph work at all"
    assert [row for row in sidecar_only if row["inside_media_guard"]] == []
    assert [row for row in sidecar_only if not row["after_terminal"]] == []
    assert terminal["derived_sync"] == "completed"
    assert "derived_sync_components" not in terminal
    assert [receipt.rel_path for receipt in deferred_index.snapshot_full(vault)] == [
        next(vault.rglob("proof.png.md")).relative_to(vault).as_posix()
    ]

    replay = writer_lease.invoke_command(
        _command("preserve_artifacts"),
        vault,
        scope="case",
        category="raw",
        files=[{"download_url": "https://files.example/p", "file_id": "file-media"}],
        adoption=adoption,
        idempotency_key=f"media-fanout-{adoption is not None}",
    )

    assert replay["derived_sync"] == "completed"
    assert replay.get("derived_sync_components") == terminal.get(
        "derived_sync_components"
    )
    assert [row for row in watch.fanouts if row["paths"] == ["proof.png.md"]] == sidecar_only


@pytest.mark.parametrize("adoption", [None, _ADOPTION], ids=["batch", "adoption"])
def test_terminal_persistence_failure_does_not_run_media_fanout(
    vault: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    adoption,  # noqa: ANN001
) -> None:
    from exomem import client_artifacts, deferred_index, writer_lease

    monkeypatch.setenv("EXOMEM_FAST_DURABLE_ACK", "0")

    staged: list[str] = []

    def stage(file, _budget, **_kwargs):  # noqa: ANN001, ANN202
        staged.append(file["file_id"])
        return _png(tmp_path, file["file_id"], "proof.png")

    monkeypatch.setattr(
        client_artifacts,
        "stage_artifact",
        stage,
    )
    manager = writer_lease.LeaseManager(
        writer_lease.LeaseConfig(state_dir=tmp_path / "state")
    )
    monkeypatch.setattr(writer_lease, "get_manager", lambda: manager)
    watch = _watch_media_fanout(monkeypatch, manager, "proof.png.md")

    original_persist = manager.idempotency._persist_completed_from_canonical
    failed = False

    def fail_terminal_receipt(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        nonlocal failed
        if not failed:
            failed = True
            raise sqlite3.OperationalError("deterministic receipt write failure")
        return original_persist(*args, **kwargs)

    monkeypatch.setattr(
        manager.idempotency, "_persist_completed_from_canonical", fail_terminal_receipt
    )

    with pytest.raises(writer_lease.OpError) as uncertain:
        writer_lease.invoke_command(
            _command("preserve_artifacts"),
            vault,
            scope="case",
            category="raw",
            files=[{"download_url": "https://files.example/p", "file_id": "file-media"}],
            adoption=adoption,
            idempotency_key=f"media-persist-failure-{adoption is not None}",
        )

    assert uncertain.value.code == "MUTATION_COMMITTED_ACKNOWLEDGEMENT_UNCERTAIN"
    assert [row for row in watch.fanouts if row["paths"] == ["proof.png.md"]] == []
    pending = deferred_index.snapshot_full(vault)
    assert [receipt.rel_path for receipt in pending] == [
        next(vault.rglob("proof.png.md")).relative_to(vault).as_posix()
    ]

    replay = writer_lease.invoke_command(
        _command("preserve_artifacts"),
        vault,
        scope="case",
        category="raw",
        files=[{"download_url": "https://files.example/p", "file_id": "file-media"}],
        adoption=adoption,
        idempotency_key=f"media-persist-failure-{adoption is not None}",
    )

    assert replay["derived_sync"] == "pending"
    assert [row for row in watch.fanouts if row["paths"] == ["proof.png.md"]] == []
    assert deferred_index.snapshot_full(vault) == pending
    assert staged == ["file-media"]


@pytest.mark.parametrize("adoption", [None, _ADOPTION], ids=["batch", "adoption"])
def test_a_fast_acknowledgement_session_carries_the_media_sidecar(
    vault: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    adoption,  # noqa: ANN001
) -> None:
    """With a derived path available, no sidecar fan-out runs on the request."""
    from exomem import client_artifacts, writer_lease

    monkeypatch.setenv("EXOMEM_FAST_DURABLE_ACK", "1")
    monkeypatch.setattr(
        client_artifacts,
        "stage_artifact",
        lambda file, _budget, **_kwargs: _png(tmp_path, file["file_id"], "proof.png"),
    )
    manager = writer_lease.LeaseManager(
        writer_lease.LeaseConfig(state_dir=tmp_path / "state")
    )
    monkeypatch.setattr(writer_lease, "get_manager", lambda: manager)
    watch = _watch_media_fanout(monkeypatch, manager, "proof.png.md")

    terminal = writer_lease.invoke_command(
        _command("preserve_artifacts"),
        vault,
        scope="case",
        category="raw",
        files=[{"download_url": "https://files.example/p", "file_id": "file-media"}],
        adoption=adoption,
        idempotency_key=f"media-derived-{adoption is not None}",
    )

    assert terminal["status"] == "committed"
    assert watch.fanouts == []
    assert terminal["derived_sync"] in {"pending", "completed", "failed"}


@pytest.mark.parametrize(
    ("component_outcome", "expected_sync", "expected_components"),
    [
        (
            None,
            "pending",
            ["embeddings", "lexstore", "memory_refs", "resolver"],
        ),
        ("deferred", "pending", ["embeddings"]),
        ("failed", "failed", ["embeddings"]),
    ],
)
def test_default_media_fanout_reports_non_graph_component_outcome(
    vault: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    component_outcome: str | None,
    expected_sync: str,
    expected_components: list[str],
) -> None:
    from exomem import client_artifacts, index_sync, writer_lease

    monkeypatch.setenv("EXOMEM_FAST_DURABLE_ACK", "0")
    monkeypatch.setattr(
        client_artifacts,
        "stage_artifact",
        lambda file, _budget, **_kwargs: _png(tmp_path, file["file_id"], "proof.png"),
    )
    manager = writer_lease.LeaseManager(
        writer_lease.LeaseConfig(state_dir=tmp_path / "state")
    )
    monkeypatch.setattr(writer_lease, "get_manager", lambda: manager)

    report = (
        index_sync.IndexSyncReport(
            "upsert",
            ("proof.png.md",),
            ("proof.png.md",),
            tuple(
                index_sync.IndexComponentOutcome(
                    component,
                    component_outcome if component == "embeddings" else "completed",
                    "test_outcome",
                )
                for component in (
                    "memory_refs",
                    "resolver",
                    "semantic_purge",
                    "lexstore",
                    "epistemic_graph",
                    "embeddings",
                )
            ),
        )
        if component_outcome is not None
        else None
    )

    def reported_fanout(
        _vault_root, _replaced, index_reports, _semantic_states, **_kwargs  # noqa: ANN001
    ) -> bool:
        assert index_reports is not None
        if report is not None:
            index_reports.append(report)
        return True

    monkeypatch.setattr(
        media_processing_module, "post_commit_batch_fanout", reported_fanout
    )

    terminal = writer_lease.invoke_command(
        _command("preserve_artifacts"),
        vault,
        scope="case",
        category="raw",
        files=[{"download_url": "https://files.example/p", "file_id": "file-media"}],
        idempotency_key=f"media-{expected_sync}",
    )

    assert terminal["derived_sync"] == expected_sync
    assert terminal["derived_sync_components"] == expected_components
    if expected_sync == "failed":
        assert terminal["derived_sync_code"] == "DERIVED_COMPONENT_FAILED"


def test_media_fanout_does_not_retire_readded_background_demand(
    vault: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import client_artifacts, deferred_index, index_sync, writer_lease

    monkeypatch.setenv("EXOMEM_FAST_DURABLE_ACK", "0")
    monkeypatch.setattr(
        client_artifacts,
        "stage_artifact",
        lambda file, _budget, **_kwargs: _png(tmp_path, file["file_id"], "proof.png"),
    )
    manager = writer_lease.LeaseManager(
        writer_lease.LeaseConfig(state_dir=tmp_path / "state")
    )
    monkeypatch.setattr(writer_lease, "get_manager", lambda: manager)

    def interleaved_fanout(
        root, replaced, index_reports, _semantic_states, **_kwargs  # noqa: ANN001
    ) -> bool:
        pending = deferred_index.snapshot_full(root)
        assert len(pending) == 1
        deferred_index.clear_full_receipts(root, pending)
        deferred_index.add_full(root, [pending[0].rel_path])
        index_reports.append(
            index_sync.IndexSyncReport(
                "upsert",
                tuple(path.relative_to(root).as_posix() for path in replaced),
                tuple(path.relative_to(root).as_posix() for path in replaced),
                tuple(
                    index_sync.IndexComponentOutcome(component, "completed", "test_completed")
                    for component in (
                        "memory_refs",
                        "resolver",
                        "semantic_purge",
                        "lexstore",
                        "epistemic_graph",
                        "embeddings",
                    )
                ),
            )
        )
        return True

    monkeypatch.setattr(
        media_processing_module, "post_commit_batch_fanout", interleaved_fanout
    )

    terminal = writer_lease.invoke_command(
        _command("preserve_artifacts"),
        vault,
        scope="case",
        category="raw",
        files=[{"download_url": "https://files.example/p", "file_id": "file-media"}],
        idempotency_key="media-fanout-aba",
    )

    assert terminal["derived_sync"] == "completed"
    assert deferred_index.snapshot_full(vault) == [
        deferred_index.DeferredReceipt(
            next(vault.rglob("proof.png.md")).relative_to(vault).as_posix(), 1
        )
    ]
