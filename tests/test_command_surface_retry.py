from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from exomem import command_surface, writer_lease
from exomem import vault as vault_module


def test_mcp_retry_scope_hashes_bearer_and_falls_back_to_session(monkeypatch) -> None:
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


def test_bound_mcp_tool_passes_retry_scope(monkeypatch) -> None:
    seen = {}

    def leaf(vault, *, value: int):  # noqa: ANN001
        return value

    command = SimpleNamespace(name="mutate", leaf=leaf, read_only=False)
    bound = command_surface.bind_vault(leaf, object(), command=command)
    monkeypatch.setattr(command_surface, "mcp_retry_scope", lambda: "bearer:abc")

    def fake_invoke(cmd, *injected, **kwargs):  # noqa: ANN001
        seen.update(command=cmd, injected=injected, kwargs=kwargs)
        return "ok"

    monkeypatch.setattr("exomem.writer_lease.invoke_command", fake_invoke)
    assert bound(value=1) == "ok"
    assert seen["kwargs"] == {"value": 1, "implicit_idempotency_scope": "bearer:abc"}


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
