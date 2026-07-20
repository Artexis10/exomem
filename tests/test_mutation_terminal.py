from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace

import pytest


def _terminal_module():
    try:
        from exomem import mutation_terminal
    except ImportError:
        pytest.fail("mutation terminal module is missing")
    return mutation_terminal


def test_compact_projection_leads_with_decisive_commit_fields() -> None:
    mutation_terminal = _terminal_module()
    raw = {
        "path": "Knowledge Base/Notes/Insights/decisive.md",
        "warnings": ["review a link"],
        "semantic": {"transition": "verbose"},
    }

    terminal = mutation_terminal.committed_terminal(
        raw,
        request_id="11111111-1111-4111-8111-111111111111",
        receipt_id="receipt-1",
        idempotency_key="public-key",
    )

    assert mutation_terminal.project_terminal(terminal, "compact") == {
        "ok": True,
        "status": "committed",
        "mutated": True,
        "path": "Knowledge Base/Notes/Insights/decisive.md",
        "request_id": "11111111-1111-4111-8111-111111111111",
        "receipt_id": "receipt-1",
        "idempotency_key": "public-key",
        "warnings_count": 1,
    }


def test_full_projection_adds_the_complete_leaf_result_only_under_diagnostics() -> None:
    mutation_terminal = _terminal_module()
    raw = {
        "paths": ["Knowledge Base/one.md", "Knowledge Base/two.md"],
        "warnings": [],
        "semantic": {"transition": "complete"},
    }
    terminal = mutation_terminal.committed_terminal(
        raw,
        request_id="11111111-1111-4111-8111-111111111111",
        receipt_id=None,
        idempotency_key=None,
    )

    projected = mutation_terminal.project_terminal(terminal, "full")

    assert projected == {
        "ok": True,
        "status": "committed",
        "mutated": True,
        "paths": ["Knowledge Base/one.md", "Knowledge Base/two.md"],
        "request_id": "11111111-1111-4111-8111-111111111111",
        "receipt_id": None,
        "warnings_count": 0,
        "diagnostics": raw,
    }
    assert "semantic" not in {key for key in projected if key != "diagnostics"}


def test_legacy_projection_returns_the_raw_leaf_result() -> None:
    mutation_terminal = _terminal_module()
    raw = {"path": "Knowledge Base/legacy.md", "semantic": {"raw": True}}
    terminal = mutation_terminal.committed_terminal(
        raw,
        request_id="11111111-1111-4111-8111-111111111111",
        receipt_id="receipt",
        idempotency_key=None,
    )

    assert mutation_terminal.project_terminal(terminal, "legacy") is raw


def test_preupgrade_raw_completed_result_is_never_fabricated_into_a_terminal() -> None:
    mutation_terminal = _terminal_module()
    raw = {"path": "Knowledge Base/pre-upgrade.md", "warnings": ["old"]}

    assert mutation_terminal.project_terminal(raw, "compact") is raw
    assert mutation_terminal.project_terminal(raw, "full") is raw
    assert mutation_terminal.project_terminal(raw, "legacy") is raw


def test_response_detail_is_removed_from_an_owned_payload_copy() -> None:
    mutation_terminal = _terminal_module()
    original = {"path": "Knowledge Base/note.md", "response_detail": "full"}

    payload, detail = mutation_terminal.split_response_detail(original)

    assert payload == {"path": "Knowledge Base/note.md"}
    assert detail == "full"
    assert original["response_detail"] == "full"


@pytest.mark.parametrize("detail", ["verbose", []])
def test_unknown_response_detail_is_rejected_before_invocation(detail) -> None:
    mutation_terminal = _terminal_module()

    with pytest.raises(ValueError, match="response_detail"):
        mutation_terminal.split_response_detail({"response_detail": detail})


def test_compound_source_result_uses_its_explicit_nested_path_and_warnings() -> None:
    mutation_terminal = _terminal_module()
    raw = {
        "source": {
            "path": "Knowledge Base/Sources/Other/source.md",
            "warnings": ["source warning"],
        },
        "compile_guidance": {"available": True},
    }

    projected = mutation_terminal.project_terminal(
        mutation_terminal.committed_terminal(
            raw,
            request_id="11111111-1111-4111-8111-111111111111",
            receipt_id=None,
            idempotency_key=None,
        )
    )

    assert projected["path"] == "Knowledge Base/Sources/Other/source.md"
    assert projected["warnings_count"] == 1


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            {"old_path": "Knowledge Base/old.md", "new_path": "Knowledge Base/new.md"},
            {"paths": ["Knowledge Base/old.md", "Knowledge Base/new.md"]},
        ),
        (
            {"restored_path": "Knowledge Base/restored.md"},
            {"path": "Knowledge Base/restored.md"},
        ),
        ({"saved": True}, {"paths": []}),
    ],
)
def test_explicit_multi_path_restore_and_safe_fallback_adapters(raw, expected) -> None:
    mutation_terminal = _terminal_module()

    projected = mutation_terminal.project_terminal(
        mutation_terminal.committed_terminal(
            raw,
            request_id="11111111-1111-4111-8111-111111111111",
            receipt_id=None,
            idempotency_key=None,
        )
    )

    assert {key: projected[key] for key in expected} == expected


def test_one_committed_identity_projects_compact_full_and_legacy_without_rerun(
    tmp_path,
) -> None:
    from exomem import writer_lease

    calls = 0
    raw = {
        "path": "Knowledge Base/Notes/Insights/once.md",
        "warnings": ["one warning"],
        "semantic": {"transition": "verbose"},
    }

    def leaf(vault, *, value: int):  # noqa: ANN001, ARG001
        nonlocal calls
        calls += 1
        writer_lease.mark_active_mutation_committed()
        return raw

    command = SimpleNamespace(name="remember", leaf=leaf, read_only=False)
    manager = writer_lease.LeaseManager(
        writer_lease.LeaseConfig(state_dir=tmp_path / "state")
    )
    first_request_id = "11111111-1111-4111-8111-111111111111"
    compact = manager.invoke(
        command,
        (tmp_path / "vault",),
        {"value": 7, "response_detail": "compact"},
        idempotency_key="same-public-key",
        idempotency_principal_scope="principal:one",
        mutation_request_id=first_request_id,
    )
    full = manager.invoke(
        command,
        (tmp_path / "vault",),
        {"value": 7, "response_detail": "full"},
        idempotency_key="same-public-key",
        idempotency_principal_scope="principal:one",
        mutation_request_id="22222222-2222-4222-8222-222222222222",
    )
    legacy = manager.invoke(
        command,
        (tmp_path / "vault",),
        {"value": 7, "response_detail": "legacy"},
        idempotency_key="same-public-key",
        idempotency_principal_scope="principal:one",
    )

    assert compact["request_id"] == first_request_id
    assert compact["receipt_id"]
    assert compact["idempotency_key"] == "same-public-key"
    assert compact["warnings_count"] == 1
    assert full == {**compact, "diagnostics": raw}
    assert legacy == raw
    assert "replayed" not in compact
    assert calls == 1


def test_acknowledgement_loss_replays_the_persisted_original_terminal(
    tmp_path,
) -> None:
    from exomem import writer_lease

    calls = 0
    interrupt = True
    raw = {"path": "Knowledge Base/Notes/Insights/ack-lost.md", "warnings": []}

    def leaf(vault):  # noqa: ANN001, ARG001
        nonlocal calls
        calls += 1
        writer_lease.mark_active_mutation_committed()
        return raw

    def after_terminal_persisted() -> None:
        nonlocal interrupt
        if interrupt:
            interrupt = False
            raise asyncio.CancelledError

    command = SimpleNamespace(name="remember", leaf=leaf, read_only=False)
    manager = writer_lease.LeaseManager(
        writer_lease.LeaseConfig(state_dir=tmp_path / "state"),
        after_terminal_persisted=after_terminal_persisted,
    )
    first_request_id = "11111111-1111-4111-8111-111111111111"
    with pytest.raises(asyncio.CancelledError):
        manager.invoke(
            command,
            (tmp_path / "vault",),
            {"response_detail": "compact"},
            idempotency_key="ack-lost",
            mutation_request_id=first_request_id,
        )

    replay = manager.invoke(
        command,
        (tmp_path / "vault",),
        {"response_detail": "full"},
        idempotency_key="ack-lost",
        mutation_request_id="22222222-2222-4222-8222-222222222222",
    )

    assert replay["request_id"] == first_request_id
    assert replay["diagnostics"] == raw
    assert calls == 1


def test_result_without_active_commit_marker_keeps_its_existing_shape(tmp_path) -> None:
    from exomem import writer_lease

    raw = {"validate_only": True, "mutated": False, "semantic": {"preview": True}}

    def preview(vault, *, validate_only: bool = False):  # noqa: ANN001, ARG001
        assert validate_only is True
        return raw

    command = SimpleNamespace(name="edit_memory", leaf=preview, read_only=False)
    manager = writer_lease.LeaseManager(
        writer_lease.LeaseConfig(state_dir=tmp_path / "state")
    )

    result = manager.invoke(
        command,
        (tmp_path / "vault",),
        {"validate_only": True, "response_detail": "full"},
        read_only=True,
    )

    assert result is raw


def test_preupgrade_completed_receipt_replays_raw_without_leaf_execution(tmp_path) -> None:
    from exomem import writer_lease

    raw = {"path": "Knowledge Base/pre-upgrade.md", "semantic": {"legacy": True}}
    command = SimpleNamespace(
        name="remember",
        leaf=lambda *_args, **_kwargs: pytest.fail("legacy receipt reran the leaf"),
        read_only=False,
    )
    manager = writer_lease.LeaseManager(
        writer_lease.LeaseConfig(state_dir=tmp_path / "state")
    )
    payload = {"value": 3}
    digest = writer_lease._command_digest(command, payload)
    key, _, _ = writer_lease._effective_idempotency_key(
        manager,
        command=command,
        mutation_subject=tmp_path / "vault",
        digest=digest,
        idempotency_key="pre-upgrade",
        principal_scope=None,
    )
    assert key is not None
    assert manager.idempotency._claim_or_inspect(key, digest, None) == ("owner", None)
    manager.idempotency._persist_completed(key, digest, raw)

    replay = manager.invoke(
        command,
        (tmp_path / "vault",),
        {**payload, "response_detail": "full"},
        idempotency_key="pre-upgrade",
    )

    assert replay == raw


def test_mutation_response_detail_is_declared_once_for_every_shared_surface() -> None:
    from exomem import cli_ops, command_surface
    from exomem.commands import product_commands_for

    command = next(
        item for item in product_commands_for("mcp") if item.name == "remember"
    )
    [parameter] = [item for item in command.params if item.name == "response_detail"]
    bound = command_surface.bind_vault(
        command.leaf,
        object(),
        name=command.name,
        command=command,
    )

    assert parameter.choices == ("compact", "full", "legacy")
    assert inspect.signature(bound).parameters["response_detail"].default == "compact"
    assert cli_ops.coerce(command.params, {"response_detail": "full"}) == {
        "response_detail": "full"
    }
