"""Contract tests for direct preservation of client-owned file handles."""

from __future__ import annotations

import asyncio
import http.client
import json
import shutil
import socket
import tempfile
import threading
import time
from contextlib import nullcontext
from pathlib import Path, PurePosixPath

import pytest

from exomem import commands


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
    assert {param.name for param in command.params} == {"scope", "category", "files", "response_detail"}
    vault_root = tmp_path / "vault"
    shutil.copytree(Path(__file__).resolve().parent / "fixtures", vault_root)
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault_root))
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "lease"))
    monkeypatch.setenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", "1")
    tool = next(
        tool
        for tool in asyncio.run(server.build_server(require_auth=False).list_tools())
        if tool.name == "preserve_artifacts"
    ).to_mcp_tool().model_dump(mode="json")

    files = tool["inputSchema"]["properties"]["files"]
    assert tool["meta"]["openai/fileParams"] == ["files"]
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


def test_bounded_retrieval_call_stops_blocked_headers_or_body() -> None:
    from exomem.client_artifacts import SafeFetchError, _bounded_retrieval_call

    release = threading.Event()
    started = time.monotonic()
    try:
        with pytest.raises(SafeFetchError, match="timed out"):
            _bounded_retrieval_call(release.wait, deadline=started + 0.01)
    finally:
        release.set()

    assert time.monotonic() - started < 0.5


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
    release = threading.Timer(0.2, peer.close)
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

    assert time.monotonic() - started < 0.1


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
    assert compact["summary"] == {"stored": 1, "failed": 1}


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
    assert compact["summary"] == {"stored": 1, "failed": 1}
    assert compact["files"][1] == {
        "file_id": "file-collision",
        "outcome": "failed",
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

    assert time.monotonic() - started < 0.5


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

    monkeypatch.setattr(
        client_artifacts,
        "stage_artifact",
        lambda *_args: pytest.fail("invalid scope must not fetch any file"),
    )

    result = commands.op_preserve_artifacts(
        tmp_path,
        scope="..",
        category="raw",
        files=[{"download_url": "https://files.example/proof", "file_id": "file-one"}],
    )

    assert result == {
        "files": [
            {
                "file_id": "file-one",
                "outcome": "failed",
                "code": "INVALID_PRESERVE",
                "reason": "scope is empty or invalid",
            }
        ],
        "summary": {"stored": 0, "failed": 1},
    }


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

    assert result["summary"] == {"stored": 0, "failed": 9}
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
    assert first["summary"] == {"stored": 8, "failed": 0}
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
    assert result["summary"] == {"stored": 1, "failed": 1}
    assert [item["file_id"] for item in result["files"]] == ["file-ok", "file-bad"]
    assert result["files"][0]["outcome"] == "stored"
    assert result["files"][1] == {
        "file_id": "file-bad",
        "outcome": "failed",
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

    assert result["summary"] == {"stored": 0, "failed": 1}
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
