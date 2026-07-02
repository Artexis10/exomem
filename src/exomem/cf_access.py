"""Cloudflare Access JWT verification for the /upload route (optional).

Inert unless BOTH `EXOMEM_CF_ACCESS_TEAM_DOMAIN` and `EXOMEM_CF_ACCESS_AUD` are
set. When configured, a request carrying a `Cf-Access-Jwt-Assertion` header is
authorized — but ONLY after the JWT's signature is verified against the team's
JWKS and its audience (the Access application's AUD tag), issuer, and expiry all
check out. We never trust the plaintext `cf-access-authenticated-user-email`
header; a spoofable string is not a credential, a verified signature is.

This is what lets the browser upload form work behind Cloudflare Access without
pasting the bearer token — securely, unlike the header-trust approach that was
removed after the security review.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def make_jwks_client(team_domain: str):
    """PyJWKClient for the team's Access certs endpoint (caches keys after first fetch)."""
    import jwt

    return jwt.PyJWKClient(f"https://{team_domain}/cdn-cgi/access/certs")


def verify(token: str | None, *, jwks_client, team_domain: str, audience: str) -> bool:
    """True iff `token` is a valid Cloudflare Access JWT for this application.

    Checks: RS256 signature against the team JWKS, `aud` == the configured AUD
    tag, `iss` == https://<team_domain>, and a live `exp`. Any failure → False
    (fail closed). `aud` and `iss` are REQUIRED — without the aud check, a token
    minted for any other Access app in the same team would otherwise pass.
    """
    if not token:
        return False
    import jwt

    try:
        signing_key = jwks_client.get_signing_key_from_jwt(token).key
        jwt.decode(
            token,
            signing_key,
            algorithms=["RS256"],
            audience=audience,
            issuer=f"https://{team_domain}",
            options={"require": ["exp", "iss", "aud"]},
        )
        return True
    except Exception as e:  # noqa: BLE001 — any verification failure must deny
        log.info("CF Access JWT rejected: %s", type(e).__name__)
        return False
