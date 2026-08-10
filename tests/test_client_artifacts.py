"""Contract tests for direct preservation of client-owned file handles."""

from __future__ import annotations

import asyncio
import io
import sys
from contextlib import nullcontext
from pathlib import Path

import pytest

from exomem import commands


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
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(tmp_path))
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
    def stage_artifact(file, _budget):
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
    monkeypatch.setattr(client_artifacts, "stage_artifact", lambda *_args: staged)
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
    from exomem import client_artifacts

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
    monkeypatch.setattr(client_artifacts, "stage_artifact", lambda *_args: staged)
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

    monkeypatch.setitem(
        sys.modules,
        "exomem.media_processing",
        type(
            "Media", (),
            {
                "classify_media": staticmethod(lambda _path: "image"),
                "reconcile_media": staticmethod(reconcile_media),
            },
        ),
    )

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
