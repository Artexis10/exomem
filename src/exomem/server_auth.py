"""OAuth wiring for Exomem's HTTP transport."""

from __future__ import annotations

import hashlib
import logging
import os
from contextlib import nullcontext
from typing import Any

import httpx
from cryptography.fernet import Fernet
from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.auth.jwt_issuer import derive_jwt_key
from fastmcp.server.auth.oauth_proxy import OAuthProxy
from fastmcp.server.auth.providers.github import GitHubTokenVerifier
from key_value.aio.stores.filetree import (
    FileTreeStore,
    FileTreeV1CollectionSanitizationStrategy,
    FileTreeV1KeySanitizationStrategy,
)
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper

from .auth_sessions import SessionAuthority
from .remote_oauth_storage import ReadThroughMirrorStorage, RemoteOAuthStorage
from .session_oauth import ExomemSessionOAuthProxy

log = logging.getLogger(__name__)


class SingleUserGitHubVerifier(GitHubTokenVerifier):
    """Use GitHub once to prove the configured login and immutable user ID."""

    def __init__(
        self,
        *,
        allowed_login: str,
        allowed_user_id: int,
        **kwargs: Any,
    ) -> None:
        login = allowed_login.strip().casefold()
        if not login:
            raise ValueError("an allowed GitHub login is required")
        if isinstance(allowed_user_id, bool) or allowed_user_id <= 0:
            raise ValueError("GitHub identity requires a positive numeric user ID")

        # A GitHub bearer is one-time identity proof. FastMCP's verifier cache
        # must stay disabled so no bearer material survives the callback.
        kwargs.pop("cache_ttl_seconds", None)
        super().__init__(cache_ttl_seconds=None, **kwargs)
        self._allowed_login = login
        self._allowed_user_id = allowed_user_id

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            async with (
                nullcontext(self._http_client)
                if self._http_client is not None
                else httpx.AsyncClient(timeout=self.timeout_seconds)
            ) as client:
                response = await client.get(
                    "https://api.github.com/user",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/vnd.github+json",
                        "User-Agent": "Exomem-GitHub-OAuth",
                    },
                )
            if response.status_code != 200:
                log.warning(
                    "GitHub identity proof failed with status %d",
                    response.status_code,
                )
                return None
            user_data = response.json()
            if not isinstance(user_data, dict):
                raise ValueError("GitHub user response must be an object")
        except (httpx.HTTPError, TypeError, ValueError) as error:
            log.warning(
                "GitHub identity proof failed due to %s",
                type(error).__name__,
            )
            return None

        login = str(user_data.get("login") or "").strip().casefold()
        raw_user_id = user_data.get("id")
        if isinstance(raw_user_id, bool):
            return None
        try:
            user_id = int(raw_user_id)
        except (TypeError, ValueError):
            return None
        if login != self._allowed_login or user_id != self._allowed_user_id:
            log.warning("rejecting GitHub token for an unexpected identity")
            return None
        return AccessToken(
            token=token,
            client_id=str(user_id),
            scopes=[],
            expires_at=None,
            claims={"sub": str(user_id), "login": login},
        )


def _required_signing_root() -> str:
    signing_root = os.environ.get("EXOMEM_JWT_SIGNING_KEY", "").strip()
    if not signing_root:
        raise RuntimeError("EXOMEM_JWT_SIGNING_KEY is required for durable OAuth sessions")
    return signing_root


def _shared_storage_settings() -> tuple[str, str, str, float] | None:
    storage_url = os.environ.get("EXOMEM_OAUTH_STORAGE_URL", "").strip()
    if not storage_url:
        if os.environ.get("EXOMEM_WRITER_LEASE_URL", "").strip():
            raise RuntimeError(
                "EXOMEM_OAUTH_STORAGE_URL is required when "
                "EXOMEM_WRITER_LEASE_URL enables HA coordination"
            )
        return None
    namespace = (
        os.environ.get("EXOMEM_OAUTH_STORAGE_NAMESPACE", "").strip()
        or os.environ.get("EXOMEM_WRITER_LEASE_VAULT_ID", "").strip()
    )
    if not namespace:
        raise RuntimeError(
            "EXOMEM_OAUTH_STORAGE_NAMESPACE or EXOMEM_WRITER_LEASE_VAULT_ID "
            "is required for shared OAuth storage"
        )
    storage_token = os.environ.get("EXOMEM_OAUTH_STORAGE_TOKEN", "").strip()
    if not storage_token:
        raise RuntimeError(
            "EXOMEM_OAUTH_STORAGE_TOKEN is required for shared OAuth storage"
        )
    timeout = float(os.environ.get("EXOMEM_OAUTH_STORAGE_TIMEOUT", "5"))
    return storage_url, namespace, storage_token, timeout


def build_session_authority(*, base_url: str) -> SessionAuthority:
    """Build the authoritative durable session store for this deployment."""
    signing_root = _required_signing_root()
    issuer = base_url.rstrip("/")
    audience = f"{issuer}/mcp"
    shared = _shared_storage_settings()
    if shared is not None:
        storage_url, namespace, storage_token, timeout = shared
        return SessionAuthority.remote(
            url=storage_url,
            namespace=namespace,
            storage_token=storage_token,
            signing_root=signing_root,
            issuer=issuer,
            audience=audience,
            timeout=timeout,
        )

    from fastmcp import settings

    return SessionAuthority.local(
        directory=settings.home / "oauth-sessions",
        signing_root=signing_root,
        issuer=issuer,
        audience=audience,
    )


def _build_oauth_client_storage(*, signing_root: str) -> Any | None:
    """Preserve FastMCP's DCR/transaction/code storage behavior in HA mode."""
    shared = _shared_storage_settings()
    if shared is None:
        return None
    storage_url, namespace, storage_token, timeout = shared
    signing_key = derive_jwt_key(
        low_entropy_material=signing_root,
        salt="fastmcp-jwt-signing-key",
    )
    storage_key = derive_jwt_key(
        high_entropy_material=signing_key.decode(),
        salt="fastmcp-storage-encryption-key",
    )
    remote = RemoteOAuthStorage(
        url=storage_url,
        namespace=namespace,
        token=storage_token,
        timeout=timeout,
        cache_ttl=float(os.environ.get("EXOMEM_OAUTH_STORAGE_CACHE_TTL", "300")),
    )

    from fastmcp import settings

    key_fingerprint = hashlib.sha256(storage_key).hexdigest()[:12]
    storage_dir = settings.home / "oauth-proxy" / key_fingerprint
    storage_dir.mkdir(parents=True, exist_ok=True)
    local = FileTreeStore(
        data_directory=storage_dir,
        key_sanitization_strategy=FileTreeV1KeySanitizationStrategy(storage_dir),
        collection_sanitization_strategy=FileTreeV1CollectionSanitizationStrategy(
            storage_dir
        ),
    )
    return FernetEncryptionWrapper(
        key_value=ReadThroughMirrorStorage(primary=remote, fallback=local),
        fernet=Fernet(key=storage_key),
        raise_on_decryption_error=False,
    )


def build_oauth(*, require_auth: bool, base_url: str) -> OAuthProxy | None:
    """Return the durable GitHub-bootstrap OAuth proxy, or None for stdio."""
    if not require_auth:
        return None

    gh_id = os.environ.get("GITHUB_CLIENT_ID", "").strip()
    gh_secret = os.environ.get("GITHUB_CLIENT_SECRET", "").strip()
    gh_username = os.environ.get("EXOMEM_GITHUB_USERNAME", "").strip()
    raw_user_id = os.environ.get("EXOMEM_GITHUB_USER_ID", "").strip()
    signing_root = os.environ.get("EXOMEM_JWT_SIGNING_KEY", "").strip()
    # Validate shared-storage trust anchors in dependency order so startup
    # reports the broken coordinator boundary directly.
    if not signing_root:
        _required_signing_root()
    if os.environ.get("EXOMEM_OAUTH_STORAGE_URL", "").strip():
        _shared_storage_settings()
    missing = [
        key
        for key, value in {
            "EXOMEM_BASE_URL": base_url,
            "GITHUB_CLIENT_ID": gh_id,
            "GITHUB_CLIENT_SECRET": gh_secret,
            "EXOMEM_GITHUB_USERNAME": gh_username,
            "EXOMEM_GITHUB_USER_ID": raw_user_id,
            "EXOMEM_JWT_SIGNING_KEY": signing_root,
        }.items()
        if not value
    ]
    if missing:
        raise RuntimeError(
            f"Missing required env vars for GitHub OAuth: {', '.join(missing)}. "
            "See README.md section Install for setup steps."
        )

    try:
        github_user_id = int(raw_user_id)
    except ValueError as error:
        raise RuntimeError(
            "EXOMEM_GITHUB_USER_ID must be a positive numeric GitHub user ID"
        ) from error
    if github_user_id <= 0:
        raise RuntimeError(
            "EXOMEM_GITHUB_USER_ID must be a positive numeric GitHub user ID"
        )

    authority = build_session_authority(base_url=base_url)
    client_storage = _build_oauth_client_storage(signing_root=signing_root)
    verifier = SingleUserGitHubVerifier(
        allowed_login=gh_username,
        allowed_user_id=github_user_id,
    )
    return ExomemSessionOAuthProxy(
        session_authority=authority,
        upstream_authorization_endpoint="https://github.com/login/oauth/authorize",
        upstream_token_endpoint="https://github.com/login/oauth/access_token",
        upstream_client_id=gh_id,
        upstream_client_secret=gh_secret,
        upstream_revocation_endpoint=None,
        token_verifier=verifier,
        base_url=base_url,
        jwt_signing_key=signing_root,
        client_storage=client_storage,
    )
