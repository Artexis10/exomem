from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import logging
import threading
import time
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware.middleware import MiddlewareContext
from mcp.types import CallToolRequest, CallToolRequestParams

from exomem import capabilities, command_surface, writer_lease
from exomem import server as server_module
from exomem import vault as vault_module
from exomem.cli_ops import OpError
from exomem.governance import principal as principal_module

#: The wall-clock shape of every contention test in this file.
#:
#: A HOLD parks a thread or process while the test observes an ordering; an
#: OBSERVATION is how long the test waits for that state to be reached. The gap
#: between them is the entire discriminating power of these tests -- a hold that
#: does not outlast its observation lets the ordering pass vacuously, and an
#: observation sized for an idle laptop fails on a loaded shard while the code
#: under test behaves correctly.
#:
#: A NEGATIVE wait (`assert not x.wait(0.1)`) proves something has NOT happened
#: yet and stays tight: widening one changes the scenario rather than merely
#: slowing it, because the product's own timeouts run in the same window.
#:
#: `join(timeout=N)` followed by `assert t.is_alive()` is the SAME negative
#: observation in join form, and it is the one shape that consumes its whole
#: window on every healthy run -- it exists to prove a competitor is still
#: parked. Widening one from 0.3s to 60s bought nothing and cost a minute a run.
#:
#: Both constants stay strictly under pytest's per-test `timeout` (pyproject
#: `[tool.pytest.ini_options]`). A valve at or above it never gets to fire: the
#: harness kills the test first and you get a thread dump where a named
#: assertion should have been. tests/test_timing_assertion_hygiene.py pins that.
#:
#: These are not latency claims. Nothing here asserts the product is fast.
_HOLD_SECONDS = 45.0
_OBSERVE_SECONDS = 15.0


@pytest.fixture(autouse=True)
def _bind_trusted_mcp_principal():
    """Direct wrapper tests model the principal installed by MCP middleware."""

    with principal_module.request_scope(
        principal_module.owner_principal(surface="mcp")
    ):
        yield


@pytest.mark.parametrize(
    ("command_name", "kwargs", "code"),
    [
        ("get", {"path": "Knowledge Base/.governance.sqlite"}, "NOT_FOUND"),
        (
            "create_file",
            {"path": "Knowledge Base/_Consolidation/run.json", "content": "secret"},
            "RESERVED_PATH",
        ),
        (
            "manage_memory_file",
            {
                "operation": "future-operation",
                "path": "Knowledge Base/.embeddings.sqlite",
            },
            "RESERVED_PATH",
        ),
        (
            "get",
            {"path": "Knowledge Base/Notes/../_Governance/rules/private.yaml"},
            "NOT_FOUND",
        ),
        (
            "create_file",
            {
                "path": "Knowledge Base/Notes/../_Governance/rules/private.yaml",
                "content": "secret",
            },
            "RESERVED_PATH",
        ),
    ],
)
def test_reserved_path_preflight_precedes_retry_and_leaf_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command_name: str,
    kwargs: dict[str, object],
    code: str,
) -> None:
    from exomem.commands import COMMANDS, PRODUCT_COMMANDS

    vault = tmp_path / "vault"
    (vault / "Knowledge Base").mkdir(parents=True)
    command = next(
        command
        for command in (*COMMANDS, *PRODUCT_COMMANDS)
        if command.name == command_name
    )
    monkeypatch.setattr(
        writer_lease,
        "get_manager",
        lambda: pytest.fail("reserved preflight reached lease/idempotency dispatch"),
    )

    with pytest.raises(OpError) as raised:
        writer_lease.invoke_command(command, vault, **kwargs)

    assert raised.value.code == code
    assert list(vault.rglob("*")) == [vault / "Knowledge Base"]


def _write_editable_note(vault: Path, *, body: str = "Before") -> str:
    relative = "Knowledge Base/Notes/Insights/retry-safe-edit.md"
    target = vault / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "---\n"
        "title: Retry-safe edit\n"
        "type: insight\n"
        "status: active\n"
        "exomem_id: 00000000-0000-4000-8000-000000000071\n"
        "---\n\n"
        f"{body}\n\n"
        "## Relations\n",
        encoding="utf-8",
    )
    return relative


def test_edit_memory_validate_only_is_read_only() -> None:
    from exomem.commands import invocation_is_read_only, product_commands_for

    command = next(
        item for item in product_commands_for("mcp") if item.name == "edit_memory"
    )

    assert invocation_is_read_only(command, {"validate_only": True}) is True
    assert invocation_is_read_only(command, {"validate_only": False}) is False
    assert invocation_is_read_only(command, {}) is False


def test_nested_edit_validate_only_is_read_only_only_for_supported_variants() -> None:
    from exomem.commands import invocation_is_read_only, product_commands_for

    command = next(
        item for item in product_commands_for("mcp") if item.name == "edit_memory"
    )

    for kind, fields in (
        ("replace_body", {"new_body": "b"}),
        ("replace_tags", {"tags": ["b"]}),
        ("replace_string", {"old_string": "a", "new_string": "b"}),
        ("batch_replace", {"edits": [{"old_string": "a", "new_string": "b"}]}),
        ("edit_section", {"heading": "Notes", "new_string": "b"}),
        ("patch_frontmatter", {"field": "domain", "value": "memory"}),
        ("fill_row", {"row_key": "Film", "take": "b"}),
    ):
        assert invocation_is_read_only(
            command, {"operation": {"kind": kind, **fields, "validate_only": True}}
        ) is True

    assert invocation_is_read_only(
        command,
        {"operation": {"kind": "replace_string", "old_string": "a", "new_string": "b"}},
    ) is False


def test_call_trace_translates_legacy_edit_on_copied_context() -> None:
    original_arguments = {
        "path": "Knowledge Base/Notes/Insights/example.md",
        "why": "legacy",
        "old_string": "Before",
        "new_string": "After",
    }
    context = MiddlewareContext(
        message=CallToolRequest(
            params=CallToolRequestParams(
                name="edit_memory", arguments=dict(original_arguments)
            )
        )
    )
    seen: list[MiddlewareContext] = []

    async def call_next(translated: MiddlewareContext) -> dict:
        seen.append(translated)
        return {"ok": True}

    with pytest.warns(DeprecationWarning):
        result = asyncio.run(
            server_module.CallTraceMiddleware().on_call_tool(context, call_next)
        )

    assert result == {"ok": True}
    assert context.message.params.arguments == original_arguments
    assert seen[0] is not context
    assert seen[0].message is not context.message
    assert seen[0].message.params.arguments == {
        "path": original_arguments["path"],
        "why": "legacy",
        "operation": {
            "kind": "replace_string",
            "old_string": "Before",
            "new_string": "After",
            "replace_all": False,
            "validate_only": False,
        },
    }


def test_equivalent_nested_and_legacy_calls_share_digest_and_execute_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict] = []

    def leaf(vault: Path, **kwargs) -> dict:  # noqa: ANN003, ARG001
        calls.append(kwargs)
        return {"path": kwargs["path"]}

    command = SimpleNamespace(name="edit_memory", leaf=leaf, read_only=False)
    manager = writer_lease.LeaseManager(
        writer_lease.LeaseConfig(state_dir=tmp_path / "state")
    )
    monkeypatch.setattr(writer_lease, "get_manager", lambda: manager)
    common = {
        "path": "Knowledge Base/Notes/Insights/example.md",
        "why": "update",
        "idempotency_key": "same-edit",
    }
    nested = writer_lease.invoke_command(
        command,
        tmp_path / "vault",
        **common,
        operation={
            "kind": "batch_replace",
            "edits": [json.dumps({"old_string": "Before", "new_string": "After"})],
        },
    )
    with pytest.warns(DeprecationWarning):
        legacy = writer_lease.invoke_command(
            command,
            tmp_path / "vault",
            **common,
            edits=[{"old_string": "Before", "new_string": "After"}],
        )

    assert nested == legacy
    assert calls == [
        {
            "path": "Knowledge Base/Notes/Insights/example.md",
            "why": "update",
            "operation": {
                "kind": "batch_replace",
                "edits": [{"old_string": "Before", "new_string": "After"}],
                "validate_only": False,
            },
        }
    ]


def test_invalid_edit_fails_before_manager_or_mutation_boundary(monkeypatch) -> None:
    command = SimpleNamespace(name="edit_memory", leaf=lambda *_a, **_k: None, read_only=False)
    monkeypatch.setattr(
        writer_lease,
        "get_manager",
        lambda: pytest.fail("invalid edit reached the writer manager"),
    )

    with pytest.raises(ValueError, match=r"INVALID_EDIT.*fill_row.*unsupported"):
        writer_lease.invoke_command(
            command,
            Path("/unused"),
            path="Knowledge Base/Notes/Insights/example.md",
            why="invalid",
            operation={
                "kind": "fill_row",
                "row_key": "Example",
                "take": "A view",
                "unsupported": "not-supported",
            },
        )


@pytest.mark.parametrize(
    ("edits", "guidance"),
    [
        ([], "at least 1 item"),
        ([{"old_string": "Before"}], "new_string"),
        (
            [{"old_string": "Before", "new_string": "After", "ignored": True}],
            "ignored",
        ),
        ([{"old_string": "Same", "new_string": "Same"}], "must differ"),
        (["not-json"], "old_string"),
    ],
)
def test_invalid_batch_edit_fails_before_manager_or_mutation_boundary(
    monkeypatch: pytest.MonkeyPatch, edits: list, guidance: str
) -> None:
    command = SimpleNamespace(name="edit_memory", leaf=lambda *_a, **_k: None, read_only=False)
    monkeypatch.setattr(
        writer_lease,
        "get_manager",
        lambda: pytest.fail("invalid batch edit reached the writer manager"),
    )

    with pytest.raises(ValueError, match="INVALID_EDIT") as exc:
        writer_lease.invoke_command(
            command,
            Path("/unused"),
            path="Knowledge Base/Notes/Insights/example.md",
            why="invalid batch",
            operation={"kind": "batch_replace", "edits": edits},
        )

    assert guidance in str(exc.value)


def test_remember_validate_only_is_read_only() -> None:
    from exomem.commands import invocation_is_read_only, product_commands_for

    command = next(
        item for item in product_commands_for("mcp") if item.name == "remember"
    )

    assert invocation_is_read_only(command, {"validate_only": True}) is True
    assert invocation_is_read_only(command, {"validate_only": False}) is False
    assert invocation_is_read_only(command, {}) is False
    # A draft commit is a write even though it carries draft identity fields.
    assert (
        invocation_is_read_only(
            command,
            {"validate_only": False, "draft_id": "d", "draft_hash": "h"},
        )
        is False
    )


def test_replace_memory_validate_only_is_read_only() -> None:
    from exomem.commands import invocation_is_read_only, product_commands_for

    command = next(
        item for item in product_commands_for("mcp") if item.name == "replace_memory"
    )

    assert invocation_is_read_only(command, {"validate_only": True}) is True
    assert invocation_is_read_only(command, {"validate_only": False}) is False
    assert invocation_is_read_only(command, {}) is False


def test_replace_preview_is_advisory_and_bound_to_predecessor_without_writes(
    vault: Path,
) -> None:
    from exomem.commands import op_replace_memory

    old_path = _write_editable_note(vault)
    before = {
        path.relative_to(vault).as_posix(): path.read_bytes()
        for path in vault.rglob("*")
        if path.is_file()
    }

    preview = op_replace_memory(
        vault,
        old_path=old_path,
        content=(
            "# Retry-safe edit v2\n\n"
            "## Observations\n\n"
            "- [runtime reliability] Keep retries bounded #retry\n"
        ),
        title="Retry-safe edit v2",
        validate_only=True,
    )

    assert preview["validate_only"] is True
    assert preview["advisory"] is True
    assert preview["committed"] is False
    assert preview["mutated"] is False
    assert preview["predecessor"] == {
        "path": old_path,
        "content_hash": hashlib.sha256((vault / old_path).read_bytes()).hexdigest(),
    }
    assert preview["draft_hash"]
    assert "receipt_id" not in preview
    after = {
        path.relative_to(vault).as_posix(): path.read_bytes()
        for path in vault.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_replace_commit_rejects_when_predecessor_changed_after_preview(
    vault: Path,
) -> None:
    from exomem.commands import op_replace_memory

    old_path = _write_editable_note(vault)
    kwargs = {
        "old_path": old_path,
        "content": (
            "# Retry-safe edit v2\n\n"
            "## Observations\n\n"
            "- [runtime reliability] Keep retries bounded #retry\n"
        ),
        "title": "Retry-safe edit v2",
    }
    preview = op_replace_memory(vault, validate_only=True, **kwargs)
    old = vault / old_path
    old.write_text(old.read_text(encoding="utf-8") + "\nConcurrent edit.\n", encoding="utf-8")

    with pytest.raises(ValueError, match="DRAFT_HASH_MISMATCH"):
        op_replace_memory(
            vault,
            draft_id=preview["draft_id"],
            draft_hash=preview["draft_hash"],
            draft_token=preview["draft_token"],
            **kwargs,
        )

    assert "status: superseded" not in old.read_text(encoding="utf-8")


def test_replace_commit_accepts_the_bound_preview_after_fresh_revalidation(
    vault: Path,
) -> None:
    from exomem.commands import op_replace_memory

    old_path = _write_editable_note(vault)
    old = vault / old_path
    old.write_bytes(old.read_bytes().replace(b"\r\n", b"\n"))
    kwargs = {
        "old_path": old_path,
        "content": (
            "# Retry-safe edit v2\n\n"
            "## Observations\n\n"
            "- [runtime reliability] Keep retries bounded #retry\n"
        ),
        "title": "Retry-safe edit v2",
    }
    preview = op_replace_memory(vault, validate_only=True, **kwargs)

    committed = op_replace_memory(
        vault,
        draft_id=preview["draft_id"],
        draft_hash=preview["draft_hash"],
        draft_token=preview["draft_token"],
        **kwargs,
    )

    assert committed["old_path"] == old_path
    assert (vault / committed["new_path"]).is_file()
    assert "status: superseded" in old.read_text(encoding="utf-8")


def test_bound_replace_preview_bypasses_writer_authority_boundary_and_receipts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.commands import product_commands_for
    from exomem.mutation_lock import VaultMutationCoordinator

    vault = tmp_path / "vault"
    vault.mkdir()
    state_dir = tmp_path / "state"
    entered = threading.Event()
    release = threading.Event()
    command = next(
        item for item in product_commands_for("mcp") if item.name == "replace_memory"
    )

    def leaf(_vault: Path, *, validate_only: bool = False) -> dict:
        assert validate_only is True
        return {
            "validate_only": True,
            "advisory": True,
            "committed": False,
            "mutated": False,
        }

    command = replace(command, leaf=leaf)
    manager = writer_lease.LeaseManager(
        writer_lease.LeaseConfig(state_dir=state_dir),
        mutation_timeout_seconds=0.05,
    )
    monkeypatch.setattr(
        manager,
        "ensure_writer",
        lambda: pytest.fail("replacement preview requested writer authority"),
    )
    monkeypatch.setattr(writer_lease, "get_manager", lambda: manager)
    monkeypatch.setattr(command_surface, "mcp_retry_scope", lambda: "session:test")
    bound = command_surface.bind_vault(leaf, vault, command=command)
    coordinator = VaultMutationCoordinator(state_dir, vault)

    def hold_live_mutation() -> None:
        with coordinator.hold(timeout_seconds=2.0):
            entered.set()
            assert release.wait(_HOLD_SECONDS)

    holder = threading.Thread(target=hold_live_mutation)
    holder.start()
    assert entered.wait(_OBSERVE_SECONDS)
    try:
        preview = bound(validate_only=True)
    finally:
        release.set()
        holder.join(timeout=_HOLD_SECONDS)

    assert preview["advisory"] is True
    assert "receipt_id" not in preview
    assert not holder.is_alive()


@pytest.mark.parametrize("operation", ("create", "append"))
def test_manage_memory_file_validate_only_is_read_only(operation: str) -> None:
    from exomem.commands import invocation_is_read_only, product_commands_for

    command = next(
        item
        for item in product_commands_for("mcp")
        if item.name == "manage_memory_file"
    )

    assert invocation_is_read_only(
        command, {"operation": operation, "validate_only": True}
    ) is True
    assert invocation_is_read_only(
        command, {"operation": operation, "validate_only": False}
    ) is False


def test_validate_only_row_edit_previews_without_mutating(vault: Path) -> None:
    from exomem.commands import op_edit_memory

    path = _write_editable_note(vault, body="- Example [take: ]")

    result = op_edit_memory(
        vault,
        path=path,
        why="preview take",
        row_key="Example",
        take="A view",
        validate_only=True,
    )

    assert result["validate_only"] is True
    assert result["row"] == "- Example [take: A view]"
    assert "[take: ]" in (vault / path).read_text(encoding="utf-8")


def test_bound_validate_only_edit_bypasses_live_mutation_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = threading.Event()
    release = threading.Event()

    def leaf(
        vault: Path,
        *,
        operation: dict,
    ) -> dict:  # noqa: ARG001
        return {"validate_only": operation["validate_only"]}

    command = SimpleNamespace(name="edit_memory", leaf=leaf, read_only=False)
    manager = writer_lease.LeaseManager(
        writer_lease.LeaseConfig(state_dir=tmp_path / "state"),
        mutation_timeout_seconds=0.05,
    )
    monkeypatch.setattr(writer_lease, "get_manager", lambda: manager)
    monkeypatch.setattr(command_surface, "mcp_retry_scope", lambda: "session:test")
    bound = command_surface.bind_vault(leaf, tmp_path / "vault", command=command)

    def hold_live_mutation() -> None:
        with manager.mutation_guard(tmp_path / "vault"):
            entered.set()
            assert release.wait(_HOLD_SECONDS)

    holder = threading.Thread(target=hold_live_mutation)
    holder.start()
    assert entered.wait(_OBSERVE_SECONDS)
    try:
        assert bound(
            operation={
                "kind": "replace_string",
                "old_string": "Before",
                "new_string": "After",
                "validate_only": True,
            }
        ) == {"validate_only": True}
    finally:
        release.set()
        holder.join(timeout=_HOLD_SECONDS)
    assert not holder.is_alive()


def test_identical_pending_retry_waits_outside_mutation_boundary_and_replays(
    tmp_path: Path,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    calls = 0
    results: list[dict] = []
    errors: list[BaseException] = []

    def leaf(vault: Path, *, value: int) -> dict:  # noqa: ARG001
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(_HOLD_SECONDS)
        return {"value": value}

    command = SimpleNamespace(name="edit_memory", leaf=leaf, read_only=False)
    (tmp_path / "vault" / "Knowledge Base").mkdir(parents=True)
    manager = writer_lease.LeaseManager(
        writer_lease.LeaseConfig(state_dir=tmp_path / "state"),
        mutation_timeout_seconds=0.05,
    )

    def invoke() -> None:
        try:
            results.append(
                manager.invoke(
                    command,
                    (tmp_path / "vault",),
                    {"value": 7},
                    implicit_idempotency_scope="session:test",
                )
            )
        except BaseException as error:  # noqa: BLE001 - assertion captures worker outcome
            errors.append(error)

    first = threading.Thread(target=invoke)
    retry = threading.Thread(target=invoke)
    first.start()
    assert entered.wait(_OBSERVE_SECONDS)
    retry.start()
    time.sleep(0.08)
    release.set()
    first.join(timeout=_HOLD_SECONDS)
    retry.join(timeout=_HOLD_SECONDS)

    assert not first.is_alive()
    assert not retry.is_alive()
    assert errors == []
    assert results == [{"value": 7}, {"value": 7}]
    assert calls == 1


def test_real_edit_semantic_preflight_failure_releases_mutation_boundary(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import semantic_writes
    from exomem.commands import product_commands_for

    path = _write_editable_note(vault)
    command = next(
        item for item in product_commands_for("mcp") if item.name == "edit_memory"
    )
    manager = writer_lease.LeaseManager(
        writer_lease.LeaseConfig(state_dir=vault.parent / "state")
    )
    bound = command_surface.bind_vault(command.leaf, vault, command=command)
    monkeypatch.setattr(writer_lease, "get_manager", lambda: manager)
    monkeypatch.setattr(command_surface, "mcp_retry_scope", lambda: "principal:test")
    kwargs = {
        "path": path,
        "why": "verify failed preflight releases writer boundary",
        "operation": {
            "kind": "replace_string",
            "old_string": "Before",
            "new_string": "After",
        },
    }
    preview = bound(
        **{
            **kwargs,
            "operation": {**kwargs["operation"], "validate_only": True},
        }
    )
    semantic = preview["semantic"]
    kwargs["operation"].update(
        transition_token=semantic["transition_token"],
        relation_disposition="reviewed_none",
        relation_review_hash=semantic["transition_hash"],
        relation_review_reason="No honest relation exists in the isolated fixture.",
    )
    original = semantic_writes.preflight_existing
    fail_once = True

    def preflight(*args, **kwargs):  # noqa: ANN002, ANN003
        nonlocal fail_once
        if fail_once:
            fail_once = False
            raise semantic_writes.SemanticWriteError(
                "STALE_SEMANTIC_WRITE", "synthetic semantic drift"
            )
        return original(*args, **kwargs)

    monkeypatch.setattr(semantic_writes, "preflight_existing", preflight)
    with pytest.raises(ValueError, match="STALE_SEMANTIC_WRITE"):
        bound(**kwargs)
    result = bound(**kwargs)

    assert result["path"] == path
    assert "After" in (vault / path).read_text(encoding="utf-8")
    # Probe the vault's real boundary: the pathless status only covers this
    # process, so it now answers `unknown` rather than a blind `free`.
    assert manager.status(vault)["mutation_boundary"]["state"] == "free"


def test_real_edit_replays_after_terminal_acknowledgement_loss(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.commands import product_commands_for

    path = _write_editable_note(vault)
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
        item for item in product_commands_for("mcp") if item.name == "edit_memory"
    )
    bound = command_surface.bind_vault(command.leaf, vault, command=command)
    monkeypatch.setattr(writer_lease, "get_manager", lambda: manager)
    monkeypatch.setattr(command_surface, "mcp_retry_scope", lambda: "principal:test")
    kwargs = {
        "path": path,
        "why": "verify edit acknowledgement replay",
        "operation": {
            "kind": "replace_string",
            "old_string": "Before",
            "new_string": "After",
        },
    }
    preview = bound(
        **{
            **kwargs,
            "operation": {**kwargs["operation"], "validate_only": True},
        }
    )
    semantic = preview["semantic"]
    kwargs["operation"].update(
        transition_token=semantic["transition_token"],
        relation_disposition="reviewed_none",
        relation_review_hash=semantic["transition_hash"],
        relation_review_reason="No honest relation exists in the isolated fixture.",
    )

    with pytest.raises(asyncio.CancelledError):
        bound(**kwargs)
    replay = bound(**kwargs)

    assert replay["path"] == path
    assert (vault / path).read_text(encoding="utf-8").count("After") == 1
    # Probe the vault's real boundary: the pathless status only covers this
    # process, so it now answers `unknown` rather than a blind `free`.
    assert manager.status(vault)["mutation_boundary"]["state"] == "free"


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
    assert invocation_is_read_only(command, {"mode": "fix"}) is True
    assert invocation_is_read_only(
        command, {"mode": "fix", "dry_run": False}
    ) is False
    assert invocation_is_read_only(command, {"mode": "reconcile"}) is False
    assert invocation_is_read_only(
        command, {"mode": "reconcile", "dry_run": True}
    ) is True


@pytest.mark.parametrize("surface", ["mcp", "rest", "hosted"])
@pytest.mark.parametrize(
    "kwargs",
    [
        {"mode": "reconcile"},
        {"mode": "fix", "dry_run": False},
        {"mode": "backfill-ids", "dry_run": False},
    ],
)
def test_remote_write_maintenance_is_refused_before_manager_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    surface: str,
    kwargs: dict[str, object],
) -> None:
    from exomem.commands import product_commands_for

    command = next(
        item for item in product_commands_for("mcp") if item.name == "maintain_memory"
    )
    descriptor = capabilities.ActiveSurfaceDescriptor(
        surface=surface,
        profile="test",
        tier2_enabled=True,
        product_commands=("maintain_memory",),
    )
    monkeypatch.setattr(
        writer_lease,
        "get_manager",
        lambda: pytest.fail("remote write maintenance reached manager dispatch"),
    )

    with capabilities.active_surface(descriptor), pytest.raises(OpError) as raised:
        writer_lease.invoke_command(command, tmp_path, **kwargs)

    assert raised.value.code == "MAINTENANCE_REQUIRES_CLI"
    assert raised.value.details == {"status": "terminal", "committed": False}
    assert raised.value.remediation is not None
    assert "exomem maintain" in raised.value.remediation


def test_hosted_agent_write_maintenance_uses_production_descriptor_and_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import hosted_gateway
    from exomem.commands import product_commands_for

    command = next(
        item for item in product_commands_for("mcp") if item.name == "maintain_memory"
    )
    descriptor = hosted_gateway.hosted_agent_surface_descriptor(
        "hosted-alpha-agent-v4"
    )
    assert descriptor.surface == "hosted-agent"
    assert "maintain_memory" in descriptor.product_commands
    monkeypatch.setattr(
        writer_lease,
        "get_manager",
        lambda: pytest.fail("hosted-agent maintenance reached manager dispatch"),
    )

    with capabilities.active_surface(descriptor), pytest.raises(OpError) as raised:
        writer_lease.invoke_command(command, tmp_path, mode="reconcile")

    assert raised.value.code == "MAINTENANCE_REQUIRES_CLI"


def test_remote_unknown_maintenance_selector_preserves_coverage_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.commands import product_commands_for

    command = next(
        item for item in product_commands_for("mcp") if item.name == "maintain_memory"
    )
    descriptor = capabilities.ActiveSurfaceDescriptor(
        surface="mcp",
        profile="test",
        tier2_enabled=True,
        product_commands=("maintain_memory",),
    )
    calls: list[str] = []

    class DispatchingManager:
        def invoke(self, dispatched, _injected, _kwargs, **_options):  # noqa: ANN001, ANN003
            calls.append(dispatched.name)
            return dispatched.leaf()

    monkeypatch.setattr(writer_lease, "get_manager", DispatchingManager)

    with capabilities.active_surface(descriptor), pytest.raises(OpError) as raised:
        writer_lease.invoke_command(command, tmp_path, mode="future-mode")

    assert raised.value.code == "RECEIPT_OUTCOME_MISSING"
    assert calls == ["maintain_memory"]


def test_mcp_write_maintenance_returns_terminal_operator_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.commands import product_commands_for

    command = next(
        item for item in product_commands_for("mcp") if item.name == "maintain_memory"
    )
    descriptor = capabilities.ActiveSurfaceDescriptor(
        surface="mcp",
        profile="test",
        tier2_enabled=True,
        product_commands=("maintain_memory",),
    )
    bound = command_surface.bind_vault(
        command.leaf,
        tmp_path,
        command=command,
        surface_descriptor=descriptor,
    )
    monkeypatch.setattr(
        writer_lease,
        "get_manager",
        lambda: pytest.fail("MCP write maintenance reached manager dispatch"),
    )

    result = bound(mode="reconcile")

    assert result["success"] is False
    assert result["error"]["code"] == "MAINTENANCE_REQUIRES_CLI"
    assert result["error"]["status"] == "terminal"
    assert result["error"]["committed"] is False
    assert "exomem maintain --reconcile" in result["error"]["remediation"]


@pytest.mark.parametrize(
    ("surface", "kwargs"),
    [
        ("mcp", {}),
        ("rest", {"mode": "fix"}),
        ("hosted", {"mode": "reconcile", "dry_run": True}),
        ("mcp", {"mode": "backfill-ids"}),
        ("cli", {"mode": "reconcile"}),
        (None, {"mode": "reconcile"}),
    ],
)
def test_remote_maintenance_reads_and_local_writes_reach_manager_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    surface: str | None,
    kwargs: dict[str, object],
) -> None:
    from contextlib import nullcontext

    from exomem.commands import product_commands_for

    command = next(
        item for item in product_commands_for("mcp") if item.name == "maintain_memory"
    )
    calls: list[dict[str, object]] = []

    class RecordingManager:
        def invoke(self, _command, _injected, call_kwargs, **options):  # noqa: ANN001, ANN003
            calls.append({"kwargs": call_kwargs, "read_only": options["read_only"]})
            return {"admitted": True}

    monkeypatch.setattr(writer_lease, "get_manager", RecordingManager)
    context = (
        nullcontext()
        if surface is None
        else capabilities.active_surface(
            capabilities.ActiveSurfaceDescriptor(
                surface=surface,
                profile="test",
                tier2_enabled=True,
                product_commands=("maintain_memory",),
            )
        )
    )

    with context:
        result = writer_lease.invoke_command(command, tmp_path, **kwargs)

    assert result == {"admitted": True}
    assert len(calls) == 1
    assert calls[0]["read_only"] is not (
        kwargs.get("mode") == "reconcile" and kwargs.get("dry_run") is not True
    )


def test_all_public_audit_routes_are_classified_read_only() -> None:
    from exomem.commands import (
        commands_for,
        invocation_is_read_only,
        product_commands_for,
    )

    audit = next(item for item in commands_for("mcp") if item.name == "audit")
    product = {item.name: item for item in product_commands_for("mcp")}

    assert invocation_is_read_only(audit, {"detail": "full"}) is True
    assert invocation_is_read_only(
        product["review_memory"], {"mode": "audit", "detail": "full"}
    ) is True
    assert invocation_is_read_only(
        product["maintain_memory"], {"mode": "audit", "detail": "full"}
    ) is True


def test_all_public_audit_routes_bypass_a_held_mutation_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.commands import commands_for, product_commands_for

    manager = writer_lease.LeaseManager(
        writer_lease.LeaseConfig(state_dir=tmp_path / "state"),
        mutation_timeout_seconds=0.0,
    )
    monkeypatch.setattr(writer_lease, "get_manager", lambda: manager)
    audit = next(item for item in commands_for("mcp") if item.name == "audit")
    product = {item.name: item for item in product_commands_for("mcp")}
    calls: list[tuple[str, dict]] = []

    def recording(command):  # noqa: ANN001, ANN202
        return replace(
            command,
            leaf=lambda _vault, **kwargs: calls.append((command.name, kwargs))
            or command.name,
        )

    routes = (
        (recording(audit), {"detail": "full"}),
        (recording(product["review_memory"]), {"mode": "audit", "detail": "full"}),
        (recording(product["maintain_memory"]), {"mode": "audit", "detail": "full"}),
    )
    vault = tmp_path / "vault"
    vault.mkdir()

    with manager.mutation_guard(vault):
        results = [
            writer_lease.invoke_command(command, vault, **kwargs)
            for command, kwargs in routes
        ]

    assert results == ["audit", "review_memory", "maintain_memory"]
    assert calls == [
        ("audit", {"detail": "full"}),
        ("review_memory", {"mode": "audit", "detail": "full"}),
        ("maintain_memory", {"mode": "audit", "detail": "full"}),
    ]
    # Probe the vault's real boundary: the pathless status only covers this
    # process, so it now answers `unknown` rather than a blind `free`.
    assert manager.status(vault)["mutation_boundary"]["state"] == "free"


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


def test_bound_mcp_tool_returns_public_operation_error_as_failure_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def leaf(vault):  # noqa: ANN001, ARG001
        raise AssertionError("invoke seam should replace the leaf")

    command = SimpleNamespace(name="mutate", leaf=leaf, read_only=False)
    bound = command_surface.bind_vault(leaf, object(), command=command)
    public_error = OpError(
        "MUTATION_BUSY",
        "vault mutation boundary is busy",
        details={
            "status": "retryable",
            "committed": False,
            "retry_after_ms": 750,
            "request_id": "req-public",
            "receipt_id": "receipt-public",
        },
    )
    monkeypatch.setattr(
        "exomem.writer_lease.invoke_command",
        lambda *args, **kwargs: (_ for _ in ()).throw(public_error),
    )

    result = bound()

    assert result["success"] is False
    assert result["error"] == public_error.as_public_dict()


def test_bound_mcp_tool_does_not_flatten_unexpected_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def leaf(vault):  # noqa: ANN001, ARG001
        raise AssertionError("invoke seam should replace the leaf")

    command = SimpleNamespace(name="mutate", leaf=leaf, read_only=False)
    bound = command_surface.bind_vault(leaf, object(), command=command)
    monkeypatch.setattr(
        "exomem.writer_lease.invoke_command",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("unexpected boom")),
    )

    with pytest.raises(RuntimeError, match="unexpected boom"):
        bound()


def test_fastmcp_busy_is_normal_tool_content_but_unexpected_fault_is_native(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def leaf(vault):  # noqa: ANN001, ARG001
        raise AssertionError("invoke seam should replace the leaf")

    command = SimpleNamespace(name="mutate", leaf=leaf, read_only=False)
    bound = command_surface.bind_vault(
        leaf, object(), name="mutate", command=command
    )
    read_command = SimpleNamespace(name="inspect", leaf=leaf, read_only=True)
    read_bound = command_surface.bind_vault(
        leaf, object(), name="inspect", command=read_command
    )
    mcp = FastMCP("application-error-contract")
    mcp.tool(bound)
    mcp.tool(read_bound)
    public_error = OpError(
        "MUTATION_BUSY",
        "vault mutation boundary is busy",
        details={
            "status": "retryable",
            "committed": False,
            "retry_after_ms": 750,
            "request_id": "req-fastmcp",
            "receipt_id": "receipt-fastmcp",
        },
    )
    failures: list[Exception] = [public_error, public_error, RuntimeError("boom")]

    def fail_in_order(*args, **kwargs):  # noqa: ANN002, ANN003
        if args[0].name == "inspect":
            return {"available": True}
        raise failures.pop(0)

    monkeypatch.setattr("exomem.writer_lease.invoke_command", fail_in_order)

    for _ in range(2):
        result = asyncio.run(mcp.call_tool("mutate", {}, run_middleware=False))
        assert result.structured_content["success"] is False
        assert result.structured_content["error"]["code"] == "MUTATION_BUSY"
        assert result.structured_content["error"]["request_id"] == "req-fastmcp"
        assert result.structured_content["error"]["receipt_id"] == "receipt-fastmcp"

    read = asyncio.run(mcp.call_tool("inspect", {}, run_middleware=False))
    assert read.structured_content == {"available": True}

    with pytest.raises(ToolError, match="boom"):
        asyncio.run(mcp.call_tool("mutate", {}, run_middleware=False))


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
        "content": (
            "# Acknowledgement-safe save\n\n"
            "## Observations\n\n"
            "- [retry guarantee] A terminal receipt makes replay deterministic.\n"
        ),
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


def test_reviewed_transition_commits_when_the_clock_ticks_between_calls(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A validated transition must survive the second changing before commit.

    `updated:` is stamped at second resolution, so recomputing it on commit
    changed the projected page and the transition check rejected the write for a
    difference nobody authored. Interactive callers hit this almost always,
    because validate and commit are separate round trips; a fast test hit it only
    when it happened to straddle a second boundary, which is why it read as a
    flake rather than a defect.
    """

    from exomem import edit as edit_module
    from exomem import temporal

    path = _write_editable_note(vault)
    ticks = iter(
        [
            dt.datetime(2026, 3, 1, 12, 0, 0, tzinfo=dt.UTC),
            dt.datetime(2026, 3, 1, 12, 0, 1, tzinfo=dt.UTC),
        ]
    )
    monkeypatch.setattr(temporal, "now", lambda: next(ticks))

    preview = edit_module.edit(
        vault,
        path=path,
        why="verify a validated transition survives a clock tick",
        old_string="Before",
        new_string="After",
        validate_only=True,
    )
    semantic = preview.as_dict()["semantic"]

    committed = edit_module.edit(
        vault,
        path=path,
        why="verify a validated transition survives a clock tick",
        old_string="Before",
        new_string="After",
        semantic_transition_token=semantic["transition_token"],
        relation_disposition="reviewed_none",
        relation_review_hash=semantic["transition_hash"],
        relation_review_reason="No honest relation exists in the isolated fixture.",
    )

    assert isinstance(committed, edit_module.EditResult)
    written = (vault / path).read_text(encoding="utf-8")
    assert "After" in written
    # The committed bytes carry the reviewed instant, not the commit instant.
    assert "updated: 2026-03-01T12:00:00Z" in written
