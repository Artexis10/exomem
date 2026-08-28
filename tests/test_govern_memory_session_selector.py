"""Generated governance-session selector and trusted-context routing."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import get_args

import pytest

from exomem import commands, reserved_paths
from exomem.command_surface import Command
from exomem.governance import authorization_session_lifecycle, operations, store
from exomem.governance.principal import RequestPrincipal

NOW = 1_800_000_000


@pytest.fixture(autouse=True)
def _governance_dispatcher_authority():
    with reserved_paths._owner_authority_scope("govern_memory"):
        yield


def _command() -> Command:
    from exomem.governance.tool import op_govern_memory

    return Command(
        name="govern_memory",
        leaf=op_govern_memory,
        params=(),
        surfaces=frozenset(),
        tier=2,
        cli_writes=True,
    )


def _context() -> authorization_session_lifecycle.AuthorizationSessionContext:
    return authorization_session_lifecycle.AuthorizationSessionContext(
        session_id="authorization-session:0123456789abcdef0123456789abcdef",
        principal_id="principal:external",
        issuer_family="mcp-oauth",
        cell_id="cell-7",
        logical_vault_id="logical-vault-7",
        keyring_id="keyring-7",
        credential_generation=2,
        expires_at=NOW + 600,
    )


def _principal(
    context: authorization_session_lifecycle.AuthorizationSessionContext | None = None,
    *,
    legacy_echo: str | None = None,
) -> RequestPrincipal:
    return RequestPrincipal(
        audience_id="principal:external",
        surface="mcp",
        issuer_family="mcp-oauth",
        authorization_session_id=legacy_echo,
        verified_authorization_session=context,
    )


def test_generated_registry_exposes_closed_session_selector() -> None:
    assert "session" in get_args(commands._GovernanceOperation)
    assert get_args(commands._GovernanceSessionAction) == (
        "open",
        "status",
        "rotate",
        "close",
    )
    assert operations.OPERATION_SPECS["session"].handler_key == "session"
    assert not operations.OPERATION_SPECS["session"].read_only
    assert not commands.invocation_is_read_only(
        _command(), {"operation": "session", "session_action": "status"}
    )


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({}, "INVALID_AUTHORIZATION_SESSION_ACTION"),
        ({"session_action": "resume"}, "INVALID_AUTHORIZATION_SESSION_ACTION"),
        ({"session_action": "open"}, "INVALID_AUTHORIZATION_SESSION_ARGUMENTS"),
        (
            {"session_action": "open", "ttl_seconds": 60, "authorization_session": "x"},
            "INVALID_AUTHORIZATION_SESSION_ARGUMENTS",
        ),
        (
            {"session_action": "status", "ttl_seconds": 60},
            "INVALID_AUTHORIZATION_SESSION_ARGUMENTS",
        ),
        (
            {"session_action": "close", "purpose": "retrieval"},
            "INVALID_AUTHORIZATION_SESSION_ARGUMENTS",
        ),
    ],
)
def test_session_arguments_refuse_before_state_creation(
    tmp_path: Path,
    kwargs: dict[str, object],
    code: str,
) -> None:
    from exomem.governance.tool import GovernanceError, op_govern_memory

    vault = tmp_path / "vault"
    with pytest.raises(GovernanceError) as error:
        op_govern_memory(vault, operation="session", principal=_principal(), **kwargs)
    assert error.value.code == code
    assert not store.sidecar_path(vault).exists()


def test_session_action_is_forbidden_on_every_other_operation(tmp_path: Path) -> None:
    from exomem.governance.tool import GovernanceError, op_govern_memory

    vault = tmp_path / "vault"
    with pytest.raises(GovernanceError) as error:
        op_govern_memory(
            vault,
            operation="list",
            session_action="open",
            principal=_principal(),
        )
    assert error.value.code == "INVALID_AUTHORIZATION_SESSION_ARGUMENTS"
    assert not store.sidecar_path(vault).exists()


def test_unverified_or_mismatched_legacy_handle_cannot_claim_session(
    tmp_path: Path,
) -> None:
    from exomem.governance.tool import GovernanceError, op_govern_memory

    vault = tmp_path / "vault"
    with pytest.raises(GovernanceError) as absent:
        op_govern_memory(
            vault,
            operation="session",
            session_action="status",
            principal=_principal(legacy_echo="caller-chosen"),
            authorization_session="caller-chosen",
        )
    assert absent.value.code == "AUTHORIZATION_SESSION_REQUIRED"

    with pytest.raises(GovernanceError) as mismatch:
        op_govern_memory(
            vault,
            operation="session",
            session_action="status",
            principal=_principal(_context(), legacy_echo="wrong"),
            authorization_session="wrong",
        )
    assert mismatch.value.code == "AUTHORIZATION_SESSION_REQUIRED"
    assert not store.sidecar_path(vault).exists()


def test_valid_open_refuses_missing_v4_store_without_creating_v3(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance import tool as governance_tool
    from exomem.governance.tool import GovernanceError

    vault = tmp_path / "vault"
    monkeypatch.setattr(
        governance_tool.authorization_custody,
        "load_authorization_custody",
        lambda _vault, *, now: object(),
    )

    with pytest.raises(GovernanceError) as error:
        governance_tool.op_govern_memory(
            vault,
            operation="session",
            session_action="open",
            ttl_seconds=60,
            principal=_principal(),
            now=NOW,
        )

    assert error.value.code == "AUTHORIZATION_SESSION_UNAVAILABLE"
    assert not store.sidecar_path(vault).exists()


class _VersionCursor:
    def fetchone(self) -> tuple[int]:
        return (4,)


class _Connection:
    closed = False

    def execute(self, statement: str) -> _VersionCursor:
        assert statement == "PRAGMA user_version"
        return _VersionCursor()

    def close(self) -> None:
        self.closed = True


def test_verified_context_routes_status_without_forwarding_raw_bearer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance import tool as governance_tool

    context = _context()
    principal = _principal(context, legacy_echo=context.session_id)
    connection = _Connection()
    custody = object()
    calls: list[tuple[object, object, int]] = []

    monkeypatch.setattr(
        governance_tool.authorization_custody,
        "load_authorization_custody",
        lambda _vault, *, now: custody,
    )
    monkeypatch.setattr(
        governance_tool.store,
        "open_authorization_session_connection",
        lambda _vault: connection,
    )

    def status_verified_session(
        active,
        *,
        custody,
        context,
        now,
    ):
        calls.append((active, context, now))
        return context

    monkeypatch.setattr(
        governance_tool.authorization_session_lifecycle,
        "status_verified_session",
        status_verified_session,
    )

    result = governance_tool.op_govern_memory(
        tmp_path / "vault",
        operation="session",
        session_action="status",
        principal=principal,
        authorization_session=context.session_id,
        now=NOW,
    )

    assert result == {
        "status": "active",
        "credential_generation": 2,
        "expires_at": datetime.fromtimestamp(NOW + 600, tz=UTC).isoformat().replace("+00:00", "Z"),
    }
    assert calls == [(connection, context, NOW)]
    assert connection.closed


@pytest.mark.parametrize(
    ("operation", "handler_name", "arguments"),
    [
        ("grant", "_grant_v4", {"token": "wh1.token"}),
        ("declare", "_declare_v4", {"purpose": "support"}),
        ("revoke", "_revoke_v4", {"scope": "session"}),
    ],
)
def test_verified_session_authoring_routes_by_internal_context_without_public_handle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    handler_name: str,
    arguments: dict[str, object],
) -> None:
    from exomem.governance import tool as governance_tool

    context = _context()
    principal = _principal(context)
    observed: list[tuple[object, object]] = []

    def bound_handler(vault_root: Path, **kwargs: object) -> dict[str, object]:
        observed.append((vault_root, kwargs["principal"]))
        assert kwargs.get("authorization_session") is None
        return {"status": "committed", "session_id": context.session_id}

    monkeypatch.setattr(governance_tool, handler_name, bound_handler, raising=False)

    result = governance_tool.op_govern_memory(
        tmp_path / "vault",
        operation=operation,
        principal=principal,
        now=NOW,
        **arguments,
    )

    assert result == {"status": "committed", "session_id": context.session_id}
    assert observed == [(tmp_path / "vault", principal)]


def test_v4_authoring_rejects_a_caller_selected_legacy_handle_before_leaf(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance import tool as governance_tool
    from exomem.governance.tool import GovernanceError

    monkeypatch.setattr(
        governance_tool.store,
        "authorization_session_schema_version",
        lambda _vault: 4,
    )
    principal = _principal(legacy_echo="caller-selected")

    with pytest.raises(GovernanceError) as error:
        governance_tool.op_govern_memory(
            tmp_path / "vault",
            operation="declare",
            principal=principal,
            authorization_session="caller-selected",
            purpose="support",
            now=NOW,
        )

    assert error.value.code == "AUTHORIZATION_SESSION_REQUIRED"


def test_missing_store_cannot_reopen_legacy_caller_handle_authority(
    tmp_path: Path,
) -> None:
    from exomem.governance import tool as governance_tool
    from exomem.governance.tool import GovernanceError

    vault = tmp_path / "vault"
    principal = _principal(legacy_echo="caller-selected")
    with pytest.raises(GovernanceError) as error:
        governance_tool.op_govern_memory(
            vault,
            operation="declare",
            principal=principal,
            authorization_session="caller-selected",
            purpose="support",
            now=NOW,
        )

    assert error.value.code == "AUTHORIZATION_SESSION_REQUIRED"
    assert not store.sidecar_path(vault).exists()


def test_verified_grant_binds_product_redemption_to_context_policy_and_membership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance import authorization_session_authority
    from exomem.governance import tool as governance_tool

    context = _context()
    principal = _principal(context)
    token_jti = "7" * 32

    class Cursor:
        def __init__(self, row: tuple[object, ...] | None = None) -> None:
            self.row = row

        def fetchone(self) -> tuple[object, ...] | None:
            return self.row

    class Connection:
        closed = False

        def execute(
            self, statement: str, _parameters: object = None
        ) -> Cursor:
            if "FROM withhold_tokens" in statement:
                return Cursor(
                    (
                        context.session_id,
                        context.principal_id,
                        context.issuer_family,
                        context.principal_id,
                        5,
                        '["' + "4" * 64 + '"]',
                        '["Notes/shared.md"]',
                        '["scope-a"]',
                        "support",
                        6,
                        "active",
                        None,
                        NOW + 300,
                        NOW - 1,
                        None,
                    )
                )
            assert statement == "BEGIN IMMEDIATE"
            return Cursor()

        def commit(self) -> None:
            pass

        def rollback(self) -> None:
            pass

        def close(self) -> None:
            self.closed = True

    connection = Connection()
    key = b"k" * 32
    custody = SimpleNamespace(keyring=SimpleNamespace(accepted_keys=()))
    review = authorization_session_authority.EscalationReview(
        authorization_session_id=context.session_id,
        principal_id=context.principal_id,
        issuer_family=context.issuer_family,
        audience=context.principal_id,
        purpose="support",
        max_level=5,
        org_ceiling=6,
        paths=("Notes/shared.md",),
        fingerprints=("4" * 64,),
        scope_ids=("scope-a",),
        expires_at=NOW + 300,
    )
    policy = SimpleNamespace(empty=False, blocked=False, fingerprint="3" * 64)
    membership = (
        authorization_session_authority.SessionMembership(
            path="Notes/shared.md",
            fingerprint="4" * 64,
            scope_ids=("scope-a", "scope-b"),
        ),
    )
    grant = authorization_session_authority.SessionGrant(
        grant_id="8" * 64,
        authorization_session_id=context.session_id,
        principal_id=context.principal_id,
        issuer_family=context.issuer_family,
        audience=context.principal_id,
        purpose="support",
        ceiling=5,
        paths=("Notes/shared.md",),
        fingerprints=("4" * 64,),
        scope_ids=("scope-a",),
        membership=membership,
        policy_fingerprint="3" * 64,
        token_jti=token_jti,
        created_at=NOW,
        expires_at=NOW + 300,
    )
    review_calls: list[dict[str, object]] = []
    prepare_calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        governance_tool,
        "_v4_authority_inputs",
        lambda _vault, _kwargs: (principal, context, custody, connection, NOW),
    )
    monkeypatch.setattr(
        governance_tool,
        "_inspect_v4_token",
        lambda *_args, **_kwargs: (review, key),
    )
    monkeypatch.setattr(governance_tool.policy_module, "load", lambda _vault: policy)
    monkeypatch.setattr(
        governance_tool,
        "_resolved_membership_manifest",
        lambda _vault, _policy, _paths: [
            {"path": "Notes/shared.md", "scope_ids": ["scope-a", "scope-b"]}
        ],
    )
    monkeypatch.setattr(
        governance_tool,
        "_content_hash",
        lambda _path: "4" * 64,
    )

    def review_escalation_redemption(
        active: object, **kwargs: object
    ) -> authorization_session_authority.SessionGrant:
        review_calls.append(kwargs)
        assert active is connection
        assert kwargs["context"] is context
        assert kwargs["signing_key"] == key
        assert kwargs["policy_fingerprint"] == "3" * 64
        assert kwargs["membership"] == membership
        return grant

    def prepare_escalation_redemption(
        active: object, **kwargs: object
    ) -> authorization_session_authority.SessionGrant:
        prepare_calls.append(kwargs)
        assert active is connection
        assert kwargs["expected_grant"] is grant
        assert kwargs["context"] is context
        assert kwargs["membership"] == membership
        return grant

    monkeypatch.setattr(
        authorization_session_authority,
        "review_escalation_redemption",
        review_escalation_redemption,
    )
    monkeypatch.setattr(
        authorization_session_authority,
        "prepare_escalation_redemption",
        prepare_escalation_redemption,
    )
    monkeypatch.setattr(
        governance_tool,
        "_create_journal",
        lambda _connection, **kwargs: {
            phase: governance_tool._composite(phase, rows)
            for phase, rows in kwargs["phases"].items()
        },
    )
    monkeypatch.setattr(governance_tool.receipts, "begin_event", lambda *_a, **_k: None)
    monkeypatch.setattr(governance_tool.receipts, "commit_event", lambda *_a, **_k: None)
    monkeypatch.setattr(governance_tool, "_arm_journal", lambda *_a, **_k: None)
    monkeypatch.setattr(governance_tool, "_activate_event", lambda *_a, **_k: None)

    result = governance_tool.op_govern_memory(
        tmp_path / "vault",
        operation="grant",
        principal=principal,
        token="wh1.bound-token",
        purpose="support",
        now=NOW,
    )

    assert result["status"] == "committed"
    assert result["grant_id"] == "8" * 64
    assert result["causation_id"]
    assert len(review_calls) == 1
    assert len(prepare_calls) == 1
    assert prepare_calls[0]["prepared_event_id"] == result["causation_id"]
    assert connection.closed
