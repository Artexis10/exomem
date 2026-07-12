from __future__ import annotations

from types import SimpleNamespace

from exomem import command_surface


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
