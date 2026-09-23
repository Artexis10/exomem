"""Every consumer of the audience sees the bound remote session as the owner.

The binding changes one thing -- the audience the MCP resolver returns -- so
each consumer is right by construction. These pin that, and pin the parts
that must NOT follow the owner: the remote issuer family, the retry scope and
the capture-sweep key. Principals come from the real resolver over a
synthetic session token; nothing here hand-builds an owner-equivalent one.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from pathlib import Path

import pytest
from fastmcp.server import dependencies as fastmcp_dependencies
from test_authorization_session_lifecycle import NOW, _connection, _custody
from test_due_state_serve_memo import _persist, _spy
from test_governance_egress import (
    RESTRICTED_PATH,
    _reset_caches,
    write_rule,
    write_scope,
)

from exomem import capture_sweep, command_surface, commands, due_state
from exomem.governance import authorization_session_lifecycle, egress
from exomem.governance.inspection import InspectionError, inspect_operation
from exomem.governance.principal import (
    OWNER_AUDIENCE,
    RequestPrincipal,
    effective_principal,
    library_scope,
    normalize_audience,
    owner_principal,
    request_scope,
    resolve_mcp_principal,
)
from exomem.governance.tool import GovernanceError, _require_owner
from exomem.session_oauth import ExomemSessionAccessToken

BASE_URL = "https://memory.example.test"
BOUND_ID = 4242
OTHER_ID = 7171
REMOTE_AUDIENCE = normalize_audience(subject=str(BOUND_ID), issuer=BASE_URL)
REMOTE_SCOPE = "principal:" + hashlib.sha256(f"{BASE_URL}\0{BOUND_ID}".encode()).hexdigest()


def _session_token(user_id: int) -> ExomemSessionAccessToken:
    return ExomemSessionAccessToken(
        token="synthetic-session-token",
        client_id="synthetic-client",
        scopes=["exomem:read"],
        claims={
            "sub": str(user_id),
            "github_user_id": user_id,
            "github_login": "example-owner",
            "iss": BASE_URL,
            "aud": f"{BASE_URL}/mcp",
        },
    )


def _resolve(monkeypatch: pytest.MonkeyPatch, user_id: int, *, bound: bool) -> RequestPrincipal:
    monkeypatch.setenv("EXOMEM_BASE_URL", BASE_URL)
    monkeypatch.setenv("EXOMEM_GITHUB_USER_ID", str(BOUND_ID))
    if bound:
        monkeypatch.setenv("EXOMEM_OWNER_OAUTH_SUBJECT", f"github:{BOUND_ID}")
    else:
        monkeypatch.delenv("EXOMEM_OWNER_OAUTH_SUBJECT", raising=False)
    token = _session_token(user_id)
    monkeypatch.setattr(fastmcp_dependencies, "get_access_token", lambda: token)
    return resolve_mcp_principal()


@pytest.fixture
def principals(monkeypatch: pytest.MonkeyPatch) -> dict[str, RequestPrincipal]:
    """The bound remote owner, its former audience, and an unrelated principal."""
    resolved = {
        "former": _resolve(monkeypatch, BOUND_ID, bound=False),
        "other": _resolve(monkeypatch, OTHER_ID, bound=True),
        "bound": _resolve(monkeypatch, BOUND_ID, bound=True),
        "local": owner_principal(surface="mcp"),
    }
    assert resolved["bound"].principal_kind == "owner-oauth"
    assert resolved["former"].audience_id == REMOTE_AUDIENCE
    assert resolved["other"].principal_kind == "principal"
    return resolved


def _get_body(vault: Path, who: RequestPrincipal) -> str | None:
    with request_scope(who):
        try:
            return commands.op_get(vault, path=RESTRICTED_PATH)["body"]
        except ValueError as error:
            assert str(error).startswith("NOT_FOUND")
            return None


def test_default_deny_scope_admits_the_bound_session_like_the_local_owner(
    vault: Path, principals: dict[str, RequestPrincipal]
) -> None:
    write_scope(vault, default_deny=True)
    _reset_caches()
    # Interleaved in one process with no cache reset between readers: the
    # owner's release must never be served to a principal that is not one.
    bound = _get_body(vault, principals["bound"])
    former = _get_body(vault, principals["former"])
    other = _get_body(vault, principals["other"])
    local = _get_body(vault, principals["local"])
    assert bound is not None and "Kill switch" in bound
    assert bound == local
    assert former is None
    assert other is None


def test_a_rule_naming_the_former_remote_audience_stops_applying(
    vault: Path, principals: dict[str, RequestPrincipal]
) -> None:
    write_scope(vault)
    write_rule(vault, ceiling=egress.LEVEL_NONE, audience=REMOTE_AUDIENCE)
    _reset_caches()
    assert _get_body(vault, principals["former"]) is None
    assert _get_body(vault, principals["bound"]) is not None


def test_owner_only_governance_admits_the_bound_session(
    principals: dict[str, RequestPrincipal],
) -> None:
    assert _require_owner(principals["bound"]) is principals["bound"]
    for name in ("former", "other"):
        with pytest.raises(GovernanceError) as raised:
            _require_owner(principals[name])
        assert raised.value.code == "GOVERNANCE_OWNER_REQUIRED"


def test_cross_audience_inspection_is_the_owner_s(
    vault: Path, principals: dict[str, RequestPrincipal]
) -> None:
    write_scope(vault)
    _reset_caches()
    result = inspect_operation(
        vault,
        "explain",
        principal=principals["bound"],
        audience="external",
        path=RESTRICTED_PATH,
    )
    # The owner-only view of the evaluation, for an audience other than the caller.
    assert "scope_contributions" in result
    with pytest.raises(InspectionError) as raised:
        inspect_operation(
            vault,
            "explain",
            principal=principals["former"],
            audience="external",
            path=RESTRICTED_PATH,
        )
    assert raised.value.code == "UNSUPPORTED_INSPECTION_AUDIENCE"


def test_due_state_serves_the_local_and_bound_owner_one_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    principals: dict[str, RequestPrincipal],
) -> None:
    due_state.reset_serve_cache()
    try:
        _persist(tmp_path)
        calls = _spy(monkeypatch)
        now = dt.datetime(2026, 9, 15, 12, tzinfo=dt.UTC)
        local = due_state.served_entries(tmp_path, now=now, principal=principals["local"])
        bound = due_state.served_entries(tmp_path, now=now, principal=principals["bound"])
        assert local == bound
        assert len(calls) == 1
        # Neither the former audience nor an unrelated principal is handed the
        # owner's memoised build.
        due_state.served_entries(tmp_path, now=now, principal=principals["former"])
        due_state.served_entries(tmp_path, now=now, principal=principals["other"])
        assert len(calls) == 3
    finally:
        due_state.reset_serve_cache()


@pytest.mark.parametrize("direction", ["local-to-remote", "remote-to-local"])
def test_an_authorization_session_never_crosses_between_owner_doors(
    principals: dict[str, RequestPrincipal], direction: str
) -> None:
    local, bound = principals["local"], principals["bound"]
    assert local.audience_id == bound.audience_id == OWNER_AUDIENCE
    assert local.issuer_family != bound.issuer_family
    opener, presenter = (local, bound) if direction == "local-to-remote" else (bound, local)
    connection, migration = _connection()
    custody = _custody(migration.activation_state_digest)
    issued = authorization_session_lifecycle.open_session(
        connection,
        custody=custody,
        principal_id=opener.audience_id,
        issuer_family=opener.issuer_family or "",
        now=NOW,
        ttl_seconds=600,
    )
    # Same door resumes...
    authorization_session_lifecycle.resume_session(
        connection,
        custody=custody,
        bearer=issued.bearer,
        principal_id=opener.audience_id,
        issuer_family=opener.issuer_family or "",
        now=NOW + 1,
    )
    # ...the other owner door is refused.
    with pytest.raises(authorization_session_lifecycle.AuthorizationSessionUnavailable):
        authorization_session_lifecycle.resume_session(
            connection,
            custody=custody,
            bearer=issued.bearer,
            principal_id=presenter.audience_id,
            issuer_family=presenter.issuer_family or "",
            now=NOW + 2,
        )


def test_retry_scope_and_capture_sweep_key_stay_the_remote_identity(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        command_surface,
        "mcp_caller_identity",
        lambda: {"transport": "http", "client_name": "remote-client"},
    )
    seen = {}
    for bound in (False, True):
        principal = _resolve(monkeypatch, BOUND_ID, bound=bound)
        seen[bound] = (
            principal.principal_kind,
            command_surface.mcp_retry_scope(),
            capture_sweep.ledger_key(vault),
        )
    assert seen[True][0] == "owner-oauth" and seen[False][0] == "principal"
    assert seen[True][1:] == seen[False][1:]
    assert seen[True][1] == REMOTE_SCOPE
    assert seen[True][2] == (REMOTE_SCOPE, "remote-client", str(vault))


def test_library_scope_under_a_bound_request_keeps_the_remote_label(
    principals: dict[str, RequestPrincipal],
) -> None:
    with request_scope(principals["bound"]):
        with library_scope():
            inner = effective_principal()
    assert inner is principals["bound"]
    assert inner.principal_kind == "owner-oauth"
    with request_scope(principals["former"]):
        with library_scope():
            assert effective_principal().audience_id == REMOTE_AUDIENCE


def test_owner_doors_share_a_preference_and_unbinding_restores_the_former_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No migration: the former remote audience's state stays intact while
    the binding is on, and comes back when it is removed."""
    from exomem import prominence_preferences as preferences

    def _as(who: RequestPrincipal, fn, *args):
        with request_scope(who):
            return fn(tmp_path, *args)

    former = _resolve(monkeypatch, BOUND_ID, bound=False)
    before = _as(former, preferences.inspect)
    _as(former, preferences.set_preference, "light", before["revision"])

    bound = _resolve(monkeypatch, BOUND_ID, bound=True)
    local = owner_principal(surface="mcp")
    owner_before = _as(local, preferences.inspect)
    assert _as(bound, preferences.inspect) == owner_before
    _as(bound, preferences.set_preference, "maximal", owner_before["revision"])
    assert _as(local, preferences.inspect)["stored"] == "maximal"

    unbound = _resolve(monkeypatch, BOUND_ID, bound=False)
    assert unbound.audience_id == REMOTE_AUDIENCE
    assert _as(unbound, preferences.inspect)["stored"] == "light"
