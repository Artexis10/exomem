"""The host's explicit binding of one remote OAuth identity as the owner.

`EXOMEM_OWNER_OAUTH_SUBJECT=github:<id>` makes a request resolve to the owner
audience only when its token came from this install's durable session proxy,
its issuer is this install's base URL, and its typed GitHub id is the bound
one. Everything else resolves exactly as before. All ids, hosts and tokens
here are synthetic.
"""

from __future__ import annotations

import hashlib
from typing import Any

import pytest
from fastmcp.server import dependencies as fastmcp_dependencies
from fastmcp.server.auth.auth import AccessToken

from exomem.governance import principal as principal_module
from exomem.governance.principal import (
    MOST_RESTRICTIVE_AUDIENCE,
    OWNER_AUDIENCE,
    RequestPrincipal,
    normalize_audience,
    owner_principal,
    resolve_mcp_principal,
)

BASE_URL = "https://memory.example.test"
BOUND_ID = 4242
OTHER_ID = 7171
ISSUER_DIGEST = hashlib.sha256(BASE_URL.encode()).hexdigest()
REMOTE_AUDIENCE = normalize_audience(subject=str(BOUND_ID), issuer=BASE_URL)


def _session_token_class() -> type:
    from exomem.session_oauth import ExomemSessionAccessToken

    return ExomemSessionAccessToken


def _claims(user_id: Any = BOUND_ID, *, iss: str = BASE_URL, sub: Any = None) -> dict[str, Any]:
    return {
        "sub": str(user_id) if sub is None else sub,
        "github_user_id": user_id,
        "github_login": "example-owner",
        "iss": iss,
        "aud": f"{iss}/mcp",
    }


def _token(claims: dict[str, Any], *, cls: type | None = None) -> AccessToken:
    token_cls = cls or _session_token_class()
    return token_cls(
        token="synthetic-session-token",
        client_id="synthetic-client",
        scopes=["exomem:read"],
        claims=claims,
    )


@pytest.fixture
def remote_host(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """A personal host that admits BOUND_ID, with no binding yet."""
    monkeypatch.setenv("EXOMEM_BASE_URL", BASE_URL)
    monkeypatch.setenv("EXOMEM_GITHUB_USER_ID", str(BOUND_ID))
    monkeypatch.delenv("EXOMEM_OWNER_OAUTH_SUBJECT", raising=False)
    return monkeypatch


def _present(monkeypatch: pytest.MonkeyPatch, token: AccessToken | None) -> None:
    monkeypatch.setattr(fastmcp_dependencies, "get_access_token", lambda: token)


# --------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------


def test_unset_binding_keeps_the_remote_principal(remote_host: pytest.MonkeyPatch) -> None:
    _present(remote_host, _token(_claims()))
    resolved = resolve_mcp_principal()
    assert resolved.audience_id == REMOTE_AUDIENCE
    assert resolved.remote_owner is False
    assert resolved.principal_kind == "principal"
    assert resolved.issuer_family == f"mcp-oauth:{ISSUER_DIGEST}"


def test_bound_session_resolves_to_owner_labelled_remote(
    remote_host: pytest.MonkeyPatch,
) -> None:
    remote_host.setenv("EXOMEM_OWNER_OAUTH_SUBJECT", f"github:{BOUND_ID}")
    _present(remote_host, _token(_claims()))
    resolved = resolve_mcp_principal()
    assert resolved.audience_id == OWNER_AUDIENCE
    assert resolved.resolved is True
    assert resolved.surface == "mcp"
    assert resolved.remote_owner is True
    assert resolved.principal_kind == "owner-oauth"
    # The remote issuer family is kept, never a local owner family.
    assert resolved.issuer_family == f"mcp-oauth:{ISSUER_DIGEST}"


def test_base_url_trailing_slash_and_whitespace_still_match(
    remote_host: pytest.MonkeyPatch,
) -> None:
    remote_host.setenv("EXOMEM_BASE_URL", f"  {BASE_URL}/ ")
    remote_host.setenv("EXOMEM_OWNER_OAUTH_SUBJECT", f" github:{BOUND_ID} ")
    _present(remote_host, _token(_claims()))
    assert resolve_mcp_principal().audience_id == OWNER_AUDIENCE


@pytest.mark.parametrize(
    "claims",
    [
        pytest.param(_claims(OTHER_ID), id="wrong-id"),
        pytest.param(_claims(iss="https://other-install.example.test"), id="wrong-issuer"),
        pytest.param(
            {k: v for k, v in _claims().items() if k != "github_user_id"}, id="missing-id"
        ),
        pytest.param(_claims(True, sub=str(BOUND_ID)), id="bool-id"),
        pytest.param(_claims(str(BOUND_ID)), id="string-id"),
        pytest.param(_claims(float(BOUND_ID), sub=str(BOUND_ID)), id="float-id"),
        pytest.param(_claims(sub=str(OTHER_ID)), id="sub-disagrees"),
        pytest.param(_claims(sub=f"github:{BOUND_ID}"), id="prefixed-sub"),
    ],
)
def test_claims_that_do_not_match_exactly_are_not_the_owner(
    remote_host: pytest.MonkeyPatch, claims: dict[str, Any]
) -> None:
    remote_host.setenv("EXOMEM_OWNER_OAUTH_SUBJECT", f"github:{BOUND_ID}")
    remote_host.setenv("EXOMEM_GITHUB_USER_ID", str(BOUND_ID))
    _present(remote_host, _token(claims))
    resolved = resolve_mcp_principal()
    assert resolved.audience_id != OWNER_AUDIENCE
    assert resolved.remote_owner is False


def test_plain_access_token_with_identical_claims_is_not_the_owner(
    remote_host: pytest.MonkeyPatch,
) -> None:
    """Provenance: only the durable session proxy's token type can match."""
    remote_host.setenv("EXOMEM_OWNER_OAUTH_SUBJECT", f"github:{BOUND_ID}")
    _present(remote_host, _token(_claims(), cls=AccessToken))
    resolved = resolve_mcp_principal()
    assert resolved.audience_id == REMOTE_AUDIENCE
    assert resolved.remote_owner is False


def test_owner_shaped_claims_without_a_verified_token_are_not_the_owner(
    remote_host: pytest.MonkeyPatch,
) -> None:
    """The header fallback (or any claims source other than a verified token)
    never reaches the binding, even with owner-shaped claims."""
    remote_host.setenv("EXOMEM_OWNER_OAUTH_SUBJECT", f"github:{BOUND_ID}")
    remote_host.setattr(
        principal_module,
        "_mcp_identity_claims",
        lambda: (_claims(), None),
    )
    resolved = resolve_mcp_principal()
    assert resolved.audience_id == REMOTE_AUDIENCE
    assert resolved.remote_owner is False


@pytest.mark.parametrize(
    "credential",
    [str(BOUND_ID), f"github:{BOUND_ID}", '{"sub": "4242", "github_user_id": 4242}'],
)
def test_forged_bearer_header_is_not_the_owner(
    remote_host: pytest.MonkeyPatch, credential: str
) -> None:
    remote_host.setenv("EXOMEM_OWNER_OAUTH_SUBJECT", f"github:{BOUND_ID}")
    _present(remote_host, None)
    remote_host.setattr(
        fastmcp_dependencies,
        "get_http_headers",
        lambda include=None: {"authorization": f"Bearer {credential}"},
    )
    resolved = resolve_mcp_principal()
    assert resolved.resolved is True
    assert resolved.audience_id not in (OWNER_AUDIENCE, MOST_RESTRICTIVE_AUDIENCE)
    assert resolved.remote_owner is False


MALFORMED_VALUES = [
    pytest.param("GitHub:4242", id="uppercase-provider"),
    pytest.param("github:04242", id="leading-zero"),
    pytest.param("github:+4242", id="sign"),
    pytest.param("github:-4242", id="negative"),
    pytest.param("github:\u0664\u0662\u0664\u0662", id="unicode-digits"),
    pytest.param("github:\uff14\uff12\uff14\uff12", id="fullwidth-digits"),
    pytest.param("github:4242 x", id="trailing-text"),
    pytest.param("github:42\n42", id="inner-newline"),
    pytest.param("github: 4242", id="inner-space"),
    pytest.param("user@example.com", id="email"),
    pytest.param("example-owner", id="login"),
    pytest.param("4242", id="no-provider"),
    pytest.param("github:", id="empty-id"),
    pytest.param("github:0", id="zero"),
    pytest.param("github:" + "1" * 20, id="twenty-digits"),
    pytest.param("gitlab:4242", id="other-provider"),
]


@pytest.mark.parametrize("value", MALFORMED_VALUES)
def test_malformed_binding_is_treated_as_unset(
    remote_host: pytest.MonkeyPatch, value: str
) -> None:
    remote_host.setenv("EXOMEM_OWNER_OAUTH_SUBJECT", value)
    _present(remote_host, _token(_claims()))
    assert principal_module.remote_owner_binding() is None
    resolved = resolve_mcp_principal()
    assert resolved.audience_id == REMOTE_AUDIENCE
    assert resolved.remote_owner is False


def test_trailing_newline_value_is_stripped_like_other_host_values(
    remote_host: pytest.MonkeyPatch,
) -> None:
    """Surrounding whitespace is the env file's, not the value's: stripped."""
    remote_host.setenv("EXOMEM_OWNER_OAUTH_SUBJECT", f"github:{BOUND_ID}\n")
    binding = principal_module.remote_owner_binding()
    assert binding is not None
    assert binding.user_id == BOUND_ID


def test_nineteen_digit_id_is_well_formed(remote_host: pytest.MonkeyPatch) -> None:
    big = int("9" * 19)
    remote_host.setenv("EXOMEM_GITHUB_USER_ID", str(big))
    remote_host.setenv("EXOMEM_OWNER_OAUTH_SUBJECT", f"github:{big}")
    binding = principal_module.remote_owner_binding()
    assert binding is not None and binding.user_id == big


def test_empty_binding_is_unset(remote_host: pytest.MonkeyPatch) -> None:
    remote_host.setenv("EXOMEM_OWNER_OAUTH_SUBJECT", "   ")
    assert principal_module.remote_owner_binding() is None


def test_binding_without_a_base_url_is_unarmed(remote_host: pytest.MonkeyPatch) -> None:
    remote_host.setenv("EXOMEM_OWNER_OAUTH_SUBJECT", f"github:{BOUND_ID}")
    remote_host.delenv("EXOMEM_BASE_URL")
    assert principal_module.remote_owner_binding() is None


def test_binding_that_is_not_the_allowed_account_never_applies(
    remote_host: pytest.MonkeyPatch,
) -> None:
    """A binding naming an account other than the one allowed to sign in is
    unreachable; it must not apply to a still-valid session of that account."""
    remote_host.setenv("EXOMEM_OWNER_OAUTH_SUBJECT", f"github:{BOUND_ID}")
    remote_host.setenv("EXOMEM_GITHUB_USER_ID", str(OTHER_ID))
    _present(remote_host, _token(_claims()))
    assert principal_module.remote_owner_binding() is None
    assert resolve_mcp_principal().audience_id == REMOTE_AUDIENCE


def test_binding_without_an_allowed_account_never_applies(
    remote_host: pytest.MonkeyPatch,
) -> None:
    remote_host.setenv("EXOMEM_OWNER_OAUTH_SUBJECT", f"github:{BOUND_ID}")
    remote_host.delenv("EXOMEM_GITHUB_USER_ID")
    assert principal_module.remote_owner_binding() is None


def test_binding_is_evaluated_on_every_request(remote_host: pytest.MonkeyPatch) -> None:
    remote_host.setenv("EXOMEM_OWNER_OAUTH_SUBJECT", f"github:{BOUND_ID}")
    _present(remote_host, _token(_claims()))
    assert resolve_mcp_principal().audience_id == OWNER_AUDIENCE
    remote_host.delenv("EXOMEM_OWNER_OAUTH_SUBJECT")
    after = resolve_mcp_principal()
    assert after.audience_id == REMOTE_AUDIENCE
    assert after.remote_owner is False


# --------------------------------------------------------------------------
# The principal's label
# --------------------------------------------------------------------------


def test_principal_kind_covers_all_four_kinds() -> None:
    assert owner_principal(surface="mcp").principal_kind == "owner"
    assert (
        RequestPrincipal(audience_id=OWNER_AUDIENCE, surface="mcp", remote_owner=True)
        .principal_kind
        == "owner-oauth"
    )
    assert RequestPrincipal(audience_id=REMOTE_AUDIENCE, surface="mcp").principal_kind == (
        "principal"
    )
    assert (
        principal_module.most_restrictive_principal(surface="mcp").principal_kind
        == "unresolved"
    )


def test_remote_owner_survives_principal_layering() -> None:
    bound = RequestPrincipal(
        audience_id=OWNER_AUDIENCE,
        surface="mcp",
        issuer_family=f"mcp-oauth:{ISSUER_DIGEST}",
        remote_owner=True,
    )
    assert bound.with_purpose("research").remote_owner is True
    assert bound.with_authorization_session("handle").remote_owner is True
    layered = bound.with_verified_authorization_session(
        None, issuer_family=f"mcp-oauth:{ISSUER_DIGEST}"
    )
    assert layered.remote_owner is True
    assert layered.principal_kind == "owner-oauth"


def test_remote_owner_is_never_produced_by_normalisation() -> None:
    """`owner` still comes only from explicit entry points."""
    for subject in (str(BOUND_ID), f"github:{BOUND_ID}", OWNER_AUDIENCE):
        assert normalize_audience(subject=subject, issuer=BASE_URL) != OWNER_AUDIENCE


# --------------------------------------------------------------------------
# Startup state (content-free)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "allowed", "expected"),
    [
        (None, str(BOUND_ID), "unset"),
        ("", str(BOUND_ID), "unset"),
        (f"github:{BOUND_ID}", str(BOUND_ID), "active"),
        (f"github:{BOUND_ID}", str(OTHER_ID), "mismatch"),
        (f"github:{BOUND_ID}", None, "mismatch"),
        ("github:01", str(BOUND_ID), "malformed"),
        ("user@example.com", str(BOUND_ID), "malformed"),
    ],
)
def test_binding_state_is_one_of_four_words(
    value: str | None, allowed: str | None, expected: str
) -> None:
    environ = {"EXOMEM_BASE_URL": BASE_URL}
    if value is not None:
        environ["EXOMEM_OWNER_OAUTH_SUBJECT"] = value
    if allowed is not None:
        environ["EXOMEM_GITHUB_USER_ID"] = allowed
    assert principal_module.remote_owner_binding_state(environ) == expected
