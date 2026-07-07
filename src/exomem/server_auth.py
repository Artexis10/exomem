"""OAuth wiring for Exomem's HTTP transport."""

from __future__ import annotations

import hashlib
import logging
import os
import time

from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.auth.oauth_proxy import OAuthProxy
from fastmcp.server.auth.providers.github import GitHubTokenVerifier

log = logging.getLogger(__name__)


class SingleUserGitHubVerifier(GitHubTokenVerifier):
    """Reject any GitHub token whose login is not the allowed user.

    Caches validated tokens for a short TTL (``EXOMEM_AUTH_CACHE_TTL`` seconds,
    default 300). Without it every MCP request, and a single connector operation
    fires several, re-hits the GitHub API to revalidate the same token.
    """

    _DEFAULT_TTL = 300.0
    _MAX_ENTRIES = 64

    def __init__(self, *, allowed_login: str, **kwargs):
        super().__init__(**kwargs)
        self._allowed_login = allowed_login.lower()
        # Do not name this ``_cache``. The parent GitHubTokenVerifier owns that
        # attribute and expects a fastmcp TokenCache instance.
        self._login_cache: dict[str, tuple[float, AccessToken]] = {}

    def _ttl(self) -> float:
        raw = os.environ.get("EXOMEM_AUTH_CACHE_TTL")
        if raw is None:
            return self._DEFAULT_TTL
        try:
            return max(0.0, float(raw))
        except ValueError:
            return self._DEFAULT_TTL

    async def verify_token(self, token: str) -> AccessToken | None:
        ttl = self._ttl()
        key = hashlib.sha256(token.encode("utf-8")).hexdigest() if token else ""
        if ttl > 0 and key:
            hit = self._login_cache.get(key)
            if hit is not None:
                ts, cached = hit
                if time.monotonic() - ts < ttl:
                    return cached
                del self._login_cache[key]

        access = await super().verify_token(token)
        if access is None:
            log.info(
                "exomem auth: token rejected by GitHub (expired/revoked/invalid); "
                "client will re-authorize"
            )
            return None
        login = (access.claims.get("login") or "").lower()
        if login != self._allowed_login:
            log.warning("rejecting token for github login=%r", login)
            return None

        if ttl > 0 and key:
            now = time.monotonic()
            if len(self._login_cache) >= self._MAX_ENTRIES:
                self._login_cache = {
                    k: v for k, v in self._login_cache.items() if now - v[0] < ttl
                }
            self._login_cache[key] = (now, access)
        return access


def build_oauth(*, require_auth: bool, base_url: str) -> OAuthProxy | None:
    """Return the GitHub OAuth proxy for HTTP transports, or None for stdio."""
    if not require_auth:
        return None

    gh_id = os.environ.get("GITHUB_CLIENT_ID", "").strip()
    gh_secret = os.environ.get("GITHUB_CLIENT_SECRET", "").strip()
    gh_username = os.environ.get("EXOMEM_GITHUB_USERNAME", "").strip()
    missing = [
        k
        for k, v in {
            "EXOMEM_BASE_URL": base_url,
            "GITHUB_CLIENT_ID": gh_id,
            "GITHUB_CLIENT_SECRET": gh_secret,
            "EXOMEM_GITHUB_USERNAME": gh_username,
        }.items()
        if not v
    ]
    if missing:
        raise RuntimeError(
            f"Missing required env vars for GitHub OAuth: {', '.join(missing)}. "
            "See README.md section Install for setup steps."
        )

    jwt_signing_key = os.environ.get("EXOMEM_JWT_SIGNING_KEY", "").strip() or None
    if jwt_signing_key is None:
        log.info(
            "EXOMEM_JWT_SIGNING_KEY not set; OAuth signing key derives from the "
            "GitHub client secret, so connector re-auth can recur on secret "
            "rotation or FastMCP upgrades. Set it in .env for a stable connector."
        )

    return OAuthProxy(
        upstream_authorization_endpoint="https://github.com/login/oauth/authorize",
        upstream_token_endpoint="https://github.com/login/oauth/access_token",
        upstream_client_id=gh_id,
        upstream_client_secret=gh_secret,
        token_verifier=SingleUserGitHubVerifier(allowed_login=gh_username),
        base_url=base_url,
        jwt_signing_key=jwt_signing_key,
        fallback_access_token_expiry_seconds=30 * 24 * 60 * 60,
    )
