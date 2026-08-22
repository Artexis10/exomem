"""Canonical audience resolution: per-surface resolvers, one id space, fail-closed.

Design decision D5. Every content-returning read resolves a canonical audience
at its surface boundary (MCP OAuth principal, REST key scope, hosted cell
principal, `owner` for stdio/CLI) and normalizes it into ONE comparable
identity space, so a grant authored against one surface matches the same human
on another. When identity *should* resolve but cannot, the decision fails
closed to the most-restrictive audience — never to the owner and never to OPEN.
"""

from __future__ import annotations

import hashlib

import pytest

from exomem.governance import principal as principal_module
from exomem.governance.principal import (
    MOST_RESTRICTIVE_AUDIENCE,
    OWNER_AUDIENCE,
    RequestPrincipal,
    current_principal,
    effective_principal,
    library_scope,
    normalize_audience,
    owner_principal,
    request_scope,
    resolve_hosted_principal,
    resolve_mcp_principal,
    resolve_rest_principal,
)

ISSUER = "https://issuer.example"
SUBJECT = "auth0|1234567890"


def _expected_oauth_audience() -> str:
    digest = hashlib.sha256(f"{ISSUER}\0{SUBJECT}".encode()).hexdigest()
    return f"principal:{digest}"


# --------------------------------------------------------------------------
# One canonical normalizer
# --------------------------------------------------------------------------


def test_normalize_audience_is_stable_over_sub_and_iss() -> None:
    assert normalize_audience(subject=SUBJECT, issuer=ISSUER) == _expected_oauth_audience()
    # Whitespace and casing of the issuer host must not fork the id space.
    assert (
        normalize_audience(subject=f"  {SUBJECT}  ", issuer=f" {ISSUER} ")
        == _expected_oauth_audience()
    )


def test_normalize_audience_without_issuer_still_canonical() -> None:
    value = normalize_audience(subject=SUBJECT, issuer=None)
    assert value.startswith("principal:")
    assert value != _expected_oauth_audience()


def test_normalize_audience_rejects_empty_subject() -> None:
    assert normalize_audience(subject="", issuer=ISSUER) == MOST_RESTRICTIVE_AUDIENCE
    assert normalize_audience(subject=None, issuer=ISSUER) == MOST_RESTRICTIVE_AUDIENCE


def test_owner_and_most_restrictive_are_distinct() -> None:
    assert OWNER_AUDIENCE == "owner"
    assert MOST_RESTRICTIVE_AUDIENCE != OWNER_AUDIENCE


# --------------------------------------------------------------------------
# Per-surface resolvers
# --------------------------------------------------------------------------


def test_cli_stdio_is_owner() -> None:
    resolved = owner_principal()
    assert resolved.audience_id == OWNER_AUDIENCE
    assert resolved.resolved is True
    assert resolved.surface == "cli"


def test_mcp_oauth_principal_resolves_from_claims(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        principal_module,
        "_mcp_identity_claims",
        lambda: ({"sub": SUBJECT, "iss": ISSUER}, None),
    )
    resolved = resolve_mcp_principal()
    assert resolved.audience_id == _expected_oauth_audience()
    assert resolved.resolved is True
    assert resolved.surface == "mcp"


def test_mcp_stdio_without_auth_is_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    """Local stdio MCP has no OAuth layer at all — that is the owner, not a
    failed resolution."""
    monkeypatch.setattr(principal_module, "_mcp_identity_claims", lambda: (None, None))
    resolved = resolve_mcp_principal()
    assert resolved.audience_id == OWNER_AUDIENCE
    assert resolved.resolved is True


def test_mcp_unresolved_but_expected_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An authenticated MCP surface that cannot resolve its expected principal
    must deny, not fall back to owner and not fall back to OPEN."""
    monkeypatch.setattr(
        principal_module, "_mcp_identity_claims", lambda: (None, "authenticated")
    )
    resolved = resolve_mcp_principal()
    assert resolved.resolved is False
    assert resolved.audience_id == MOST_RESTRICTIVE_AUDIENCE
    assert resolved.audience_id != OWNER_AUDIENCE


def test_rest_api_key_scope_is_owner() -> None:
    """`_rest_principal` returns `None` for the vault's own shared REST key —
    that is the owner's key, not an unresolved principal."""
    resolved = resolve_rest_principal(None)
    assert resolved.audience_id == OWNER_AUDIENCE
    assert resolved.resolved is True
    assert resolved.surface == "rest"


def test_rest_cf_access_scope_normalizes_to_oauth_id_space() -> None:
    digest = hashlib.sha256(f"{ISSUER}\0{SUBJECT}".encode()).hexdigest()
    resolved = resolve_rest_principal(f"cf-access:{digest}")
    assert resolved.audience_id == _expected_oauth_audience()
    assert resolved.resolved is True


def test_same_human_matches_across_mcp_and_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The load-bearing cross-surface invariant: a grant authored for this
    principal on MCP applies to the same human arriving over REST."""
    monkeypatch.setattr(
        principal_module,
        "_mcp_identity_claims",
        lambda: ({"sub": SUBJECT, "iss": ISSUER}, None),
    )
    mcp = resolve_mcp_principal()
    digest = hashlib.sha256(f"{ISSUER}\0{SUBJECT}".encode()).hexdigest()
    rest = resolve_rest_principal(f"cf-access:{digest}")
    assert mcp.audience_id == rest.audience_id


def test_hosted_cell_principal_scope() -> None:
    resolved = resolve_hosted_principal("principal-scope-abc")
    assert resolved.resolved is True
    assert resolved.surface == "hosted"
    assert resolved.audience_id not in (OWNER_AUDIENCE, MOST_RESTRICTIVE_AUDIENCE)


def test_hosted_missing_principal_fails_closed() -> None:
    """The hosted cell surface always expects a gateway principal scope; a
    missing one is a resolution failure, never the owner."""
    resolved = resolve_hosted_principal(None)
    assert resolved.resolved is False
    assert resolved.audience_id == MOST_RESTRICTIVE_AUDIENCE


def test_hosted_principal_is_stable_for_same_scope() -> None:
    a = resolve_hosted_principal("principal-scope-abc")
    b = resolve_hosted_principal("principal-scope-abc")
    c = resolve_hosted_principal("principal-scope-xyz")
    assert a.audience_id == b.audience_id
    assert a.audience_id != c.audience_id


def test_trusted_surface_resolvers_bind_closed_issuer_families(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        principal_module,
        "_mcp_identity_claims",
        lambda: ({"sub": SUBJECT, "iss": ISSUER}, None),
    )
    issuer_digest = hashlib.sha256(ISSUER.encode()).hexdigest()

    assert resolve_mcp_principal().issuer_family == f"mcp-oauth:{issuer_digest}"
    assert resolve_rest_principal(None).issuer_family == "rest-api-key"
    assert (
        resolve_rest_principal("cf-access:principal-digest").issuer_family
        == "rest-cf-access"
    )
    assert (
        resolve_hosted_principal("principal-scope-abc").issuer_family
        == "hosted-gateway"
    )
    assert owner_principal(surface="cli").issuer_family == "cli-local-owner"


# --------------------------------------------------------------------------
# Contextvar scope (clone of capabilities.active_surface)
# --------------------------------------------------------------------------


def test_request_scope_binds_and_resets() -> None:
    assert current_principal() is None
    who = RequestPrincipal(audience_id="external", surface="mcp")
    with request_scope(who):
        assert current_principal() is who
    assert current_principal() is None


def test_request_scope_nests_and_restores() -> None:
    outer = RequestPrincipal(audience_id="outer", surface="rest")
    inner = RequestPrincipal(audience_id="inner", surface="mcp")
    with request_scope(outer):
        with request_scope(inner):
            assert current_principal() is inner
        assert current_principal() is outer
    assert current_principal() is None


def test_request_scope_resets_on_exception() -> None:
    who = RequestPrincipal(audience_id="external", surface="rest")
    with pytest.raises(RuntimeError):  # noqa: PT012 - the reset is the assertion
        with request_scope(who):
            raise RuntimeError("boom")
    assert current_principal() is None


def test_effective_principal_fails_closed_when_unbound() -> None:
    """M15: the unbound default used to be `owner`, i.e. full disclosure for
    any code path that forgot `request_scope`.

    "No surface bound a principal" and "a surface forgot to bind one" are the
    same observation from inside this function, and one of them is a total
    bypass of the release plane. The safe reading of "nobody said who is
    asking" is therefore the fail-closed floor. Genuine owner-local callers
    say so with `library_scope()`.
    """
    assert current_principal() is None
    who = effective_principal()
    assert who.audience_id == MOST_RESTRICTIVE_AUDIENCE
    assert who.resolved is False
    assert who.audience_id != OWNER_AUDIENCE


def test_library_scope_is_the_explicit_owner_opt_in() -> None:
    """The replacement for the implicit default: an in-process owner-local
    caller declares itself instead of inheriting full disclosure by omission."""
    assert current_principal() is None
    with library_scope():
        who = effective_principal()
        assert who.audience_id == OWNER_AUDIENCE
        assert who.resolved is True
        assert who.surface == "library"
    assert current_principal() is None


def test_library_scope_does_not_override_a_bound_surface_principal() -> None:
    """A library helper called *underneath* a bound remote request must not
    escalate it back to the owner — that would re-open M15 from the inside."""
    remote = RequestPrincipal(audience_id="external", surface="mcp")
    with request_scope(remote), library_scope():
        assert effective_principal() is remote


def test_every_dispatch_site_binds_a_principal() -> None:
    """What keeps the fail-closed default from being a nuisance instead of a
    guarantee: every module that calls the shared dispatcher must bind a
    principal in the same module. A new surface that dispatches without
    binding now returns nothing rather than everything — and this names it."""
    import pathlib

    src = pathlib.Path(__file__).resolve().parent.parent / "src" / "exomem"
    offenders = []
    for path in sorted(src.rglob("*.py")):
        if path.name == "writer_lease.py":
            continue
        text = path.read_text(encoding="utf-8")
        if "invoke_command(" not in text:
            continue
        if "request_scope(" not in text:
            offenders.append(path.name)
    assert offenders == []


def test_effective_principal_returns_bound_principal() -> None:
    who = RequestPrincipal(audience_id="external", surface="mcp", purpose="audit")
    with request_scope(who):
        assert effective_principal() is who


def test_purpose_travels_on_the_principal() -> None:
    who = RequestPrincipal(audience_id="external", surface="mcp", purpose="due-diligence")
    with request_scope(who):
        assert effective_principal().purpose == "due-diligence"


def test_with_purpose_returns_a_new_principal() -> None:
    """`purpose` arrives as a per-call leaf param, not from the surface — so
    it layers onto the bound principal without mutating it."""
    who = RequestPrincipal(audience_id="external", surface="mcp")
    layered = who.with_purpose("audit")
    assert layered.purpose == "audit"
    assert layered.audience_id == who.audience_id
    assert who.purpose is None


def test_with_purpose_none_keeps_existing() -> None:
    who = RequestPrincipal(audience_id="external", surface="mcp", purpose="audit")
    assert who.with_purpose(None).purpose == "audit"


def test_request_principal_is_frozen() -> None:
    who = RequestPrincipal(audience_id="external", surface="mcp")
    with pytest.raises(Exception):  # noqa: B017, PT011 - dataclass FrozenInstanceError
        who.audience_id = "owner"  # type: ignore[misc]


# --------------------------------------------------------------------------
# Set-points: the surface adapters must actually bind the contextvar
# --------------------------------------------------------------------------


def test_bind_vault_wrapper_preserves_dispatcher_bound_mcp_principal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The raw dispatcher is the set-point; wrappers consume trusted context."""
    from exomem import command_surface

    monkeypatch.setattr(
        principal_module,
        "_mcp_identity_claims",
        lambda: ({"sub": SUBJECT, "iss": ISSUER}, None),
    )
    seen: list[RequestPrincipal | None] = []

    def leaf(vault_root: str, value: str = "x") -> str:
        seen.append(current_principal())
        return value

    wrapper = command_surface.bind_vault(leaf, "/vault")
    principal = resolve_mcp_principal()
    with request_scope(principal):
        assert wrapper(value="ok") == "ok"
    assert seen and seen[0] is not None
    assert seen[0].audience_id == _expected_oauth_audience()
    assert seen[0].surface == "mcp"
    # And the binding is torn down when the invocation ends.
    assert current_principal() is None


def test_bind_vault_wrapper_refuses_unbound_stdio_adapter() -> None:
    from exomem import command_surface
    from exomem.governance.authorization_request import AuthorizationContextUnavailable

    def leaf(vault_root: str) -> str:
        return "ok"

    with pytest.raises(AuthorizationContextUnavailable):
        command_surface.bind_vault(leaf, "/vault")()


def test_bind_vault_wrapper_refuses_unbound_remote_adapter() -> None:
    from exomem import command_surface
    from exomem.governance.authorization_request import AuthorizationContextUnavailable

    def leaf(vault_root: str) -> str:
        return "ok"

    with pytest.raises(AuthorizationContextUnavailable):
        command_surface.bind_vault(leaf, "/vault")()
