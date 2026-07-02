"""cf_access.verify — Cloudflare Access JWT validation (real RSA, no network)."""

from __future__ import annotations

import datetime as dt

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from exomem import cf_access

TEAM = "myteam.cloudflareaccess.com"
AUD = "abc123aud"


def _key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


class _FakeJWKS:
    """Stands in for PyJWKClient — returns a fixed public key, no network."""

    def __init__(self, public_key) -> None:
        self._pub = public_key

    def get_signing_key_from_jwt(self, token):  # noqa: ARG002
        return type("K", (), {"key": self._pub})()


def _make(priv, *, aud=AUD, iss=f"https://{TEAM}", exp_delta=3600, drop=None) -> str:
    now = dt.datetime.now(dt.timezone.utc)
    payload = {
        "aud": aud,
        "iss": iss,
        "exp": now + dt.timedelta(seconds=exp_delta),
        "iat": now,
        "sub": "user@example.com",
    }
    if drop:
        payload.pop(drop, None)
    return jwt.encode(payload, priv, algorithm="RS256")


def _verify(token, priv_for_jwks) -> bool:
    jwks = _FakeJWKS(priv_for_jwks.public_key())
    return cf_access.verify(token, jwks_client=jwks, team_domain=TEAM, audience=AUD)


def test_valid_token_passes() -> None:
    priv = _key()
    assert _verify(_make(priv), priv) is True


def test_none_token_fails() -> None:
    assert _verify(None, _key()) is False


def test_wrong_audience_fails() -> None:
    priv = _key()
    assert _verify(_make(priv, aud="some-other-app"), priv) is False


def test_wrong_issuer_fails() -> None:
    priv = _key()
    assert _verify(_make(priv, iss="https://evil.cloudflareaccess.com"), priv) is False


def test_expired_token_fails() -> None:
    priv = _key()
    assert _verify(_make(priv, exp_delta=-10), priv) is False


def test_missing_aud_claim_fails() -> None:
    priv = _key()
    assert _verify(_make(priv, drop="aud"), priv) is False


def test_signature_from_other_key_fails() -> None:
    signer, attacker = _key(), _key()
    # signed by `signer`, but the JWKS hands back `attacker`'s public key
    assert _verify(_make(signer), attacker) is False
