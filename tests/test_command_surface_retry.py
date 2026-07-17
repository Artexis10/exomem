from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from exomem import command_surface, writer_lease
from exomem import server as server_module
from exomem import vault as vault_module


def test_mcp_retry_scope_hashes_bearer_and_falls_back_to_session(monkeypatch) -> None:
    monkeypatch.setattr(
        "fastmcp.server.dependencies.get_access_token",
        lambda: None,
    )
    monkeypatch.setattr(
        "fastmcp.server.dependencies.get_http_headers",
        lambda **kwargs: {"authorization": "Bearer super-secret-token"},
    )
    monkeypatch.setattr(
        "fastmcp.server.dependencies.get_context",
        lambda: SimpleNamespace(session_id="session-1"),
    )
    scope = command_surface.mcp_retry_scope()
    assert scope.startswith("bearer:")
    assert "super-secret-token" not in scope

    monkeypatch.setattr("fastmcp.server.dependencies.get_http_headers", lambda **kwargs: {})
    assert command_surface.mcp_retry_scope() == "session:session-1"


def test_mcp_retry_scope_uses_stable_verified_principal_across_bearer_rotation(
    monkeypatch,
) -> None:
    token = SimpleNamespace(
        token="first-bearer",
        client_id="chat-client",
        claims={"iss": "https://memory.example", "sub": "user-42"},
    )
    monkeypatch.setattr(
        "fastmcp.server.dependencies.get_access_token",
        lambda: token,
    )
    monkeypatch.setattr(
        "fastmcp.server.dependencies.get_http_headers",
        lambda **kwargs: {"authorization": f"Bearer {token.token}"},
    )
    monkeypatch.setattr(
        "fastmcp.server.dependencies.get_context",
        lambda: SimpleNamespace(session_id="session-1"),
    )

    first = command_surface.mcp_retry_scope()
    token.token = "rotated-bearer"
    second = command_surface.mcp_retry_scope()

    assert first == second
    assert first.startswith("principal:")
    assert "user-42" not in first


def test_maintain_audit_is_read_only_for_common_invocation_boundary() -> None:
    from exomem.commands import invocation_is_read_only, product_commands_for

    command = next(
        item for item in product_commands_for("mcp") if item.name == "maintain_memory"
    )
    assert invocation_is_read_only(command, {}) is True
    assert invocation_is_read_only(command, {"mode": "audit"}) is True
    assert invocation_is_read_only(command, {"mode": "fix"}) is False
    assert invocation_is_read_only(command, {"mode": "reconcile"}) is False


def test_bound_mcp_tool_passes_retry_scope(monkeypatch) -> None:
    seen = {}

    def leaf(vault, *, value: int):  # noqa: ANN001
        return value

    command = SimpleNamespace(name="mutate", leaf=leaf, read_only=False)
    bound = command_surface.bind_vault(leaf, object(), command=command)
    monkeypatch.setattr(command_surface, "mcp_retry_scope", lambda: "bearer:abc")
    monkeypatch.setattr(
        command_surface,
        "mcp_request_id",
        lambda: "11111111-1111-4111-8111-111111111111",
    )

    def fake_invoke(cmd, *injected, **kwargs):  # noqa: ANN001
        seen.update(command=cmd, injected=injected, kwargs=kwargs)
        return "ok"

    monkeypatch.setattr("exomem.writer_lease.invoke_command", fake_invoke)
    assert bound(value=1) == "ok"
    assert seen["kwargs"] == {
        "value": 1,
        "implicit_idempotency_scope": "bearer:abc",
        "mutation_request_id": "11111111-1111-4111-8111-111111111111",
    }


def test_mcp_request_id_prefers_edge_correlation_header(monkeypatch) -> None:
    expected = "11111111-1111-4111-8111-111111111111"
    monkeypatch.setattr(
        "fastmcp.server.dependencies.get_http_headers",
        lambda **kwargs: {"x-exomem-request-id": expected},
    )
    assert command_surface.mcp_request_id() == expected


def test_mcp_request_id_rejects_non_uuid_header(monkeypatch) -> None:
    generated = uuid.UUID("22222222-2222-4222-8222-222222222222")
    monkeypatch.setattr(
        "fastmcp.server.dependencies.get_http_headers",
        lambda **kwargs: {"x-exomem-request-id": "attacker supplied log content"},
    )
    monkeypatch.setattr(command_surface.uuid, "uuid4", lambda: generated)

    assert command_surface.mcp_request_id() == str(generated)


def test_call_trace_and_bound_tool_share_generated_request_id(
    monkeypatch, caplog: pytest.LogCaptureFixture
) -> None:
    generated = iter(
        [
            uuid.UUID("11111111-1111-4111-8111-111111111111"),
            uuid.UUID("22222222-2222-4222-8222-222222222222"),
        ]
    )
    seen: list[str] = []
    monkeypatch.setattr(
        "fastmcp.server.dependencies.get_http_headers", lambda **kwargs: {}
    )
    monkeypatch.setattr(command_surface.uuid, "uuid4", lambda: next(generated))
    middleware = server_module.CallTraceMiddleware()
    context = SimpleNamespace(message={"params": {"name": "remember", "arguments": {}}})

    async def call_next(_context):  # noqa: ANN001
        seen.append(command_surface.mcp_request_id())
        return {"ok": True}

    with caplog.at_level(logging.INFO, logger="exomem.calls"):
        asyncio.run(middleware.on_call_tool(context, call_next))

    expected = "11111111-1111-4111-8111-111111111111"
    assert seen == [expected]
    assert f"request_id={expected}" in caplog.text


def test_bound_process_media_status_skips_mutation_retry_scope(
    tmp_path: Path, monkeypatch
) -> None:
    from exomem.commands import product_commands_for

    command = next(
        command for command in product_commands_for("mcp") if command.name == "process_media"
    )
    bound = command_surface.bind_vault(command.leaf, tmp_path, command=command)
    scopes: list[str] = []
    calls: list[dict] = []
    monkeypatch.setattr(
        command_surface,
        "mcp_retry_scope",
        lambda: scopes.append("requested") or "bearer:abc",
    )

    def fake_invoke(cmd, *injected, **kwargs):  # noqa: ANN001
        calls.append(kwargs)
        return {"operation": kwargs["operation"]}

    monkeypatch.setattr("exomem.writer_lease.invoke_command", fake_invoke)

    assert bound(operation="status") == {"operation": "status"}
    assert calls[-1]["implicit_idempotency_scope"] is None
    assert scopes == []
    assert bound(operation="process") == {"operation": "process"}
    assert calls[-1]["implicit_idempotency_scope"] == "bearer:abc"
    assert scopes == ["requested"]


def test_bound_mcp_committed_failure_replays_sanitized_without_second_leaf_call(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    raw = PermissionError(
        f"{tmp_path}/.exomem-batch-{'a' * 32}/stage-0.tmp: private low-level detail"
    )
    original = vault_module.BatchWriteError(
        "BATCH_CLEANUP_INCOMPLETE",
        vault_module.BatchTargetSummary(1, ("note.md",), 0),
        committed=True,
        diagnostics=(raw,),
    )

    def leaf(vault):  # noqa: ANN001, ARG001
        nonlocal calls
        calls += 1
        raise original from raw

    command = SimpleNamespace(name="mutate", leaf=leaf, read_only=False)
    manager = writer_lease.LeaseManager(writer_lease.LeaseConfig(state_dir=tmp_path))
    monkeypatch.setattr(writer_lease, "get_manager", lambda: manager)
    monkeypatch.setattr(command_surface, "mcp_retry_scope", lambda: "session:one")
    bound = command_surface.bind_vault(leaf, object(), command=command)

    with pytest.raises(vault_module.BatchWriteError) as first:
        bound()
    with pytest.raises(ValueError) as replay:
        bound()

    assert first.value.as_public_dict() == original.as_public_dict()
    assert replay.value.as_public_dict() == original.as_public_dict()
    assert str(first.value) == str(replay.value)
    for secret in (str(tmp_path), ".exomem-batch-", "stage-0.tmp", "low-level detail"):
        assert secret not in str(first.value)
        assert secret not in str(replay.value)
    assert calls == 1


def test_bound_remember_replays_after_terminal_acknowledgement_loss(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.commands import op_remember, product_commands_for

    kwargs = {
        "content": "# Acknowledgement-safe save\n\nOne deterministic conclusion.\n",
        "title": "Acknowledgement-safe save",
        "slug": "acknowledgement-safe-save",
        "suggestions": False,
    }
    validation = op_remember(vault, validate_only=True, **kwargs)
    kwargs.update(
        draft_id=validation["draft_id"],
        draft_hash=validation["draft_hash"],
        draft_token=validation["draft_token"],
        relation_disposition="reviewed_none",
        relation_review_hash=validation["draft_hash"],
        relation_review_reason="No honest relation exists in the isolated fixture.",
    )
    interrupt = True

    def after_terminal_persisted() -> None:
        nonlocal interrupt
        if interrupt:
            interrupt = False
            raise asyncio.CancelledError

    manager = writer_lease.LeaseManager(
        writer_lease.LeaseConfig(state_dir=vault.parent / "state"),
        after_terminal_persisted=after_terminal_persisted,
    )
    command = next(
        item for item in product_commands_for("mcp") if item.name == "remember"
    )
    bound = command_surface.bind_vault(command.leaf, vault, command=command)
    monkeypatch.setattr(writer_lease, "get_manager", lambda: manager)
    monkeypatch.setattr(command_surface, "mcp_retry_scope", lambda: "principal:test")

    with pytest.raises(asyncio.CancelledError):
        bound(**kwargs)
    replay = bound(**kwargs)

    assert replay["path"] == validation["destination"]
    assert (vault / replay["path"]).is_file()
    assert list((vault / replay["path"]).parent.glob("acknowledgement-safe-save*.md")) == [
        vault / replay["path"]
    ]
