"""Closed credential routing and bearer-free trusted request context."""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import commands
from exomem.governance import authorization_session_lifecycle
from exomem.governance.principal import RequestPrincipal

NOW = 1_800_000_000
BEARER = "as1." + "A" * 22 + "." + "B" * 43


class _Connection:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _principal() -> RequestPrincipal:
    return RequestPrincipal(
        audience_id="principal:external",
        surface="mcp",
        resolved=True,
        issuer_family="mcp-oauth:issuer-a",
    )


def _context() -> authorization_session_lifecycle.AuthorizationSessionContext:
    return authorization_session_lifecycle.AuthorizationSessionContext(
        session_id="authorization-session:0123456789abcdef0123456789abcdef",
        principal_id="principal:external",
        issuer_family="mcp-oauth:issuer-a",
        cell_id="cell-7",
        logical_vault_id="logical-vault-7",
        keyring_id="keyring-7",
        credential_generation=1,
        expires_at=NOW + 600,
    )


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({"operation": "session", "session_action": "open"}, "forbidden"),
        ({"operation": "session", "session_action": "status"}, "required"),
        ({"operation": "session", "session_action": "rotate"}, "required"),
        ({"operation": "session", "session_action": "close"}, "required"),
        ({"operation": "grant"}, "required"),
        ({"operation": "grant", "scope": "standing"}, "non_authorizing"),
        ({"operation": "revoke"}, "required"),
        ({"operation": "revoke", "scope": "standing"}, "non_authorizing"),
        ({"operation": "declare"}, "required"),
        ({"operation": "list"}, "optional"),
        ({"operation": "explain"}, "optional"),
        ({"operation": "simulate"}, "optional"),
        ({"operation": "propose"}, "non_authorizing"),
        ({"operation": "commit"}, "non_authorizing"),
        ({"operation": "suspend"}, "non_authorizing"),
        ({"operation": "resume"}, "non_authorizing"),
        ({"operation": "undo"}, "non_authorizing"),
        (
            {"operation": "backfill_companion", "backfill_action": "preview"},
            "non_authorizing",
        ),
        (
            {"operation": "backfill_companion", "backfill_action": "commit"},
            "non_authorizing",
        ),
    ],
)
def test_governance_variants_have_one_closed_credential_rule(
    arguments: dict[str, object], expected: str
) -> None:
    from exomem.governance.authorization_request import credential_rule

    assert credential_rule("govern_memory", arguments).value == expected


def test_every_non_governance_product_command_is_classified_optional() -> None:
    from exomem.governance.authorization_request import credential_rule

    for command in commands.PRODUCT_COMMANDS:
        if command.name != "govern_memory":
            assert credential_rule(command.name, {}).value == "optional"


def test_every_legacy_leaf_is_classified_and_registry_validation_is_total() -> None:
    from exomem.governance.authorization_request import (
        AuthorizationRouteUnclassified,
        credential_rule,
        validate_credential_registry,
    )

    registered = {
        command.name for command in (*commands.PRODUCT_COMMANDS, *commands.COMMANDS)
    }
    for command_name in sorted(registered - {"govern_memory"}):
        assert credential_rule(command_name, {}).value == "optional"

    validate_credential_registry()
    with pytest.raises(AuthorizationRouteUnclassified):
        validate_credential_registry((*registered, "future_unclassified_command"))


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"operation": "session"},
        {"operation": "session", "session_action": "resume"},
        {"operation": "grant", "scope": "typo"},
        {"operation": "revoke", "scope": "typo"},
        {"operation": "backfill_companion"},
        {"operation": "backfill_companion", "backfill_action": "typo"},
        {"operation": "unknown"},
    ],
)
def test_unknown_governance_variant_has_no_fallback(arguments: dict[str, object]) -> None:
    from exomem.governance.authorization_request import (
        AuthorizationRouteUnclassified,
        credential_rule,
    )

    with pytest.raises(AuthorizationRouteUnclassified):
        credential_rule("govern_memory", arguments)


def test_absent_optional_credential_binds_no_session_without_loading_custody(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem.governance import authorization_request

    monkeypatch.setattr(
        authorization_request.authorization_custody,
        "load_authorization_custody",
        lambda *_args, **_kwargs: pytest.fail("optional absence loaded custody"),
    )
    bound = authorization_request.bind_authorization_context(
        tmp_path,
        principal=_principal(),
        credential=authorization_request.ABSENT_CREDENTIAL,
        rule=authorization_request.CredentialRule.OPTIONAL,
        now=NOW,
    )

    assert bound.verified_authorization_session is None
    assert bound.authorization_session_id is None
    assert bound.issuer_family == "mcp-oauth:issuer-a"


@pytest.mark.parametrize("rule", ["required", "forbidden"])
def test_missing_required_or_present_forbidden_refuses_before_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rule: str,
) -> None:
    from exomem.governance import authorization_request

    monkeypatch.setattr(
        authorization_request.authorization_custody,
        "load_authorization_custody",
        lambda *_args, **_kwargs: pytest.fail("refused request loaded custody"),
    )
    credential = (
        authorization_request.ABSENT_CREDENTIAL if rule == "required" else BEARER
    )
    with pytest.raises(authorization_request.AuthorizationContextUnavailable) as error:
        authorization_request.bind_authorization_context(
            tmp_path,
            principal=_principal(),
            credential=credential,
            rule=authorization_request.CredentialRule(rule),
            now=NOW,
        )
    assert str(error.value) == "authorization session is unavailable"


def test_valid_bearer_becomes_internal_context_and_server_derived_legacy_echo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance import authorization_request

    context = _context()
    custody = object()
    connection = _Connection()
    calls: list[tuple[object, object, str, str, int]] = []
    monkeypatch.setattr(
        authorization_request.authorization_custody,
        "load_authorization_custody",
        lambda _vault, *, now: custody,
    )
    monkeypatch.setattr(
        authorization_request.store,
        "open_authorization_session_connection",
        lambda _vault: connection,
    )

    def resume_session(
        active,
        *,
        custody,
        bearer,
        principal_id,
        issuer_family,
        now,
    ):
        calls.append((active, custody, principal_id, issuer_family, now))
        assert bearer == BEARER
        return context

    monkeypatch.setattr(
        authorization_request.authorization_session_lifecycle,
        "resume_session",
        resume_session,
    )

    bound = authorization_request.bind_authorization_context(
        tmp_path,
        principal=_principal(),
        credential=BEARER,
        rule=authorization_request.CredentialRule.REQUIRED,
        now=NOW,
    )

    assert bound.verified_authorization_session is context
    assert bound.authorization_session_id == context.session_id
    assert BEARER not in repr(bound)
    assert calls == [
        (connection, custody, "principal:external", "mcp-oauth:issuer-a", NOW)
    ]
    assert connection.closed


def test_invalid_present_credential_has_one_content_free_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance import authorization_request

    connection = _Connection()
    monkeypatch.setattr(
        authorization_request.authorization_custody,
        "load_authorization_custody",
        lambda _vault, *, now: object(),
    )
    monkeypatch.setattr(
        authorization_request.store,
        "open_authorization_session_connection",
        lambda _vault: connection,
    )
    monkeypatch.setattr(
        authorization_request.authorization_session_lifecycle,
        "resume_session",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            authorization_session_lifecycle.AuthorizationSessionUnavailable()
        ),
    )

    with pytest.raises(authorization_request.AuthorizationContextUnavailable) as error:
        authorization_request.bind_authorization_context(
            tmp_path,
            principal=_principal(),
            credential="not-a-bearer",
            rule=authorization_request.CredentialRule.OPTIONAL,
            now=NOW,
        )
    assert error.value.code == "AUTHORIZATION_SESSION_UNAVAILABLE"
    assert str(error.value) == "authorization session is unavailable"
    assert "not-a-bearer" not in repr(error.value)
    assert connection.closed
