"""FastMCP 3.4.4 adapter for durable Exomem-owned OAuth sessions."""

from __future__ import annotations

import logging
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import httpx
from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.auth.oauth_proxy import OAuthProxy
from fastmcp.server.auth.oauth_proxy.models import (
    DEFAULT_AUTH_CODE_EXPIRY_SECONDS,
    HTTP_TIMEOUT_SECONDS,
    ClientCode,
)
from fastmcp.server.auth.oauth_proxy.ui import create_error_html
from mcp.server.auth.handlers.metadata import MetadataHandler
from mcp.server.auth.provider import AuthorizationCode, RefreshToken, TokenError
from mcp.server.auth.routes import build_metadata, cors_middleware
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Route
from typing_extensions import override

from .auth_sessions import SessionAuthority, SessionIdentity, SessionStoreUnavailable

logger = logging.getLogger(__name__)


class SessionStoreUnavailableMiddleware:
    """Map only authoritative session-store failures to retryable HTTP 503."""

    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            await self.app(scope, receive, send)
        except SessionStoreUnavailable:
            if scope.get("type") != "http":
                raise
            logger.warning("durable auth session store unavailable")
            response = JSONResponse(
                {
                    "error": "temporarily_unavailable",
                    "error_description": (
                        "The authentication session store is temporarily unavailable; retry shortly."
                    ),
                },
                status_code=503,
                headers={"Retry-After": "5"},
            )
            await response(scope, receive, send)


class ExomemSessionOAuthProxy(OAuthProxy):
    """Use GitHub once for identity, then issue and validate Exomem sessions."""

    def __init__(
        self,
        *,
        session_authority: SessionAuthority,
        github_cleanup_transport: httpx.AsyncBaseTransport | None = None,
        **kwargs: Any,
    ):
        if kwargs.get("upstream_revocation_endpoint") is not None:
            raise ValueError(
                "durable sessions use local RFC 7009 revocation, not an upstream endpoint"
            )
        kwargs["upstream_revocation_endpoint"] = None
        super().__init__(**kwargs)
        self._session_authority = session_authority
        self._github_cleanup_transport = github_cleanup_transport
        self.revocation_options = RevocationOptions(enabled=True)

    @override
    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: AuthorizationCode,
    ) -> OAuthToken:
        code_model = await self._code_store.get(key=authorization_code.code)
        if code_model is None:
            raise TokenError("invalid_grant", "Authorization code not found")
        if code_model.client_id != client.client_id:
            raise TokenError("invalid_grant", "Authorization code client mismatch")

        consumed = await self._code_store.delete(key=authorization_code.code)
        if not consumed:
            raise TokenError("invalid_grant", "Authorization code not found")

        proof = code_model.idp_tokens.get("exomem_identity")
        if not isinstance(proof, dict):
            raise TokenError("invalid_grant", "Authorization identity proof is missing")
        try:
            raw_user_id = proof["github_user_id"]
            if isinstance(raw_user_id, bool):
                raise ValueError("boolean identity IDs are invalid")
            identity = SessionIdentity(
                github_user_id=int(raw_user_id),
                github_login=str(proof["github_login"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise TokenError(
                "invalid_grant", "Authorization identity proof is invalid"
            ) from error

        if client.client_id is None:
            raise TokenError("invalid_client", "Client ID is required")
        scopes = list(code_model.scopes)
        bearer, _ = await self._session_authority.issue(
            client_id=client.client_id,
            scopes=scopes,
            identity=identity,
        )
        return OAuthToken(
            access_token=bearer,
            token_type="Bearer",
            expires_in=None,
            scope=" ".join(scopes) or None,
            refresh_token=None,
        )

    @override
    async def load_access_token(self, token: str) -> AccessToken | None:
        record = await self._session_authority.validate(token)
        if record is None:
            return None
        return AccessToken(
            token=token,
            client_id=record.client_id,
            scopes=list(record.scopes),
            expires_at=None,
            resource=record.audience,
            claims={
                "sub": str(record.github_user_id),
                "github_user_id": record.github_user_id,
                "github_login": record.github_login,
                "iss": record.issuer,
                "aud": record.audience,
            },
        )

    @override
    async def load_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: str,
    ) -> RefreshToken | None:
        """Refuse refresh without consulting preserved FastMCP legacy state."""
        del client, refresh_token
        return None

    @override
    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        """Defensively prevent callers from re-entering FastMCP's legacy flow."""
        del client, refresh_token, scopes
        raise TokenError("invalid_grant", "Refresh tokens are not supported")

    @override
    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        """Register authorization-code-only downstream clients."""
        client_info.grant_types = ["authorization_code"]
        await super().register_client(client_info)

    @override
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        """Hide refresh grants retained in bounded rollback client records."""
        client = await super().get_client(client_id)
        if client is None:
            return None
        return client.model_copy(update={"grant_types": ["authorization_code"]})

    @override
    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        record = await self._session_authority.validate(token.token)
        if record is None:
            return
        await self._session_authority.tombstone(
            record.session_id,
            reason="oauth-client-revocation",
        )

    @override
    def get_middleware(self) -> list:
        return [
            Middleware(SessionStoreUnavailableMiddleware),
            *super().get_middleware(),
        ]

    @override
    def get_routes(self, mcp_path: str | None = None) -> list[Route]:
        """Preserve FastMCP routes while advertising no refresh-token grant."""
        routes = super().get_routes(mcp_path)
        rewritten: list[Route] = []
        for route in routes:
            if not route.path.startswith("/.well-known/oauth-authorization-server"):
                rewritten.append(route)
                continue

            client_options = (
                self.client_registration_options or ClientRegistrationOptions()
            )
            metadata = build_metadata(
                self.base_url,
                self.service_documentation_url,
                client_options,
                self.revocation_options or RevocationOptions(),
            )
            metadata.grant_types_supported = ["authorization_code"]
            if self._cimd_manager is not None:
                metadata.client_id_metadata_document_supported = True
                existing = metadata.token_endpoint_auth_methods_supported or []
                metadata.token_endpoint_auth_methods_supported = [
                    *existing,
                    "private_key_jwt",
                    "none",
                ]
            handler = MetadataHandler(metadata)
            rewritten.append(
                Route(
                    path=route.path,
                    endpoint=cors_middleware(handler.handle, ["GET", "OPTIONS"]),
                    methods=route.methods or ["GET", "OPTIONS"],
                    name=route.name,
                    include_in_schema=route.include_in_schema,
                )
            )
        return rewritten

    async def _cleanup_github_token(self, access_token: str) -> None:
        if self._upstream_client_secret is None:
            logger.warning("GitHub OAuth token cleanup skipped: missing application secret")
            return
        endpoint = (
            "https://api.github.com/applications/"
            f"{self._upstream_client_id}/token"
        )
        try:
            async with httpx.AsyncClient(
                timeout=HTTP_TIMEOUT_SECONDS,
                transport=self._github_cleanup_transport,
            ) as client:
                response = await client.request(
                    "DELETE",
                    endpoint,
                    auth=(
                        self._upstream_client_id,
                        self._upstream_client_secret.get_secret_value(),
                    ),
                    json={"access_token": access_token},
                    headers={"Accept": "application/vnd.github+json"},
                )
            if response.status_code in {204, 404}:
                return
            logger.warning(
                "GitHub OAuth token cleanup failed with status %d",
                response.status_code,
            )
        except httpx.HTTPError as error:
            logger.warning(
                "GitHub OAuth token cleanup failed due to %s",
                type(error).__name__,
            )

    @staticmethod
    def _verified_identity_proof(verified: AccessToken | None) -> dict[str, Any] | None:
        if verified is None:
            return None
        claims = verified.claims or {}
        login = str(claims.get("login") or "").strip().casefold()
        raw_user_id = claims.get("sub") or verified.client_id
        if isinstance(raw_user_id, bool):
            return None
        try:
            user_id = int(raw_user_id)
        except (TypeError, ValueError):
            return None
        if not login or user_id <= 0:
            return None
        return {
            "exomem_identity": {
                "github_user_id": user_id,
                "github_login": login,
            }
        }

    @staticmethod
    def _error_response(message: str, *, status_code: int) -> HTMLResponse:
        return HTMLResponse(
            content=create_error_html(
                error_title="OAuth Error",
                error_message=message,
            ),
            status_code=status_code,
        )

    @override
    async def _handle_idp_callback(
        self, request: Request
    ) -> HTMLResponse | RedirectResponse:
        """Exchange, verify, dispose, and retain only a minimal identity proof."""
        try:
            idp_code = request.query_params.get("code")
            txn_id = request.query_params.get("state")
            error = request.query_params.get("error")

            if not idp_code and not error:
                return self._error_response(
                    "Missing authorization code or transaction ID from the identity provider.",
                    status_code=400,
                )

            transaction_model = (
                await self._transaction_store.get(key=txn_id) if txn_id else None
            )

            if error:
                error_description = request.query_params.get("error_description")
                if transaction_model:
                    client_redirect_uri = transaction_model.client_redirect_uri
                    if not self._validate_client_redirect_uri(client_redirect_uri):
                        return self._error_response(
                            "Invalid redirect URI",
                            status_code=400,
                        )
                    error_params: dict[str, str] = {
                        "error": error,
                        "state": transaction_model.client_state,
                    }
                    if error_description:
                        error_params["error_description"] = error_description
                    separator = "&" if "?" in client_redirect_uri else "?"
                    return RedirectResponse(
                        url=f"{client_redirect_uri}{separator}{urlencode(error_params)}",
                        status_code=302,
                    )
                return self._error_response(
                    f"Authentication failed: {error_description or 'Unknown error'}",
                    status_code=400,
                )

            if not txn_id or transaction_model is None:
                return self._error_response(
                    "Invalid or expired authorization transaction. Please try authenticating again.",
                    status_code=400,
                )
            if not self._validate_client_redirect_uri(
                transaction_model.client_redirect_uri
            ):
                return self._error_response("Invalid redirect URI", status_code=400)

            if self._require_authorization_consent in (True, "remember"):
                consent_token = transaction_model.consent_token
                if not consent_token:
                    return self._error_response(
                        "Invalid authorization flow. Please try authenticating again.",
                        status_code=403,
                    )
                if not self._verify_consent_binding_cookie(
                    request, txn_id, consent_token
                ):
                    return self._error_response(
                        "Authorization session mismatch. Please try authenticating again.",
                        status_code=403,
                    )

            transaction = transaction_model.model_dump()
            idp_redirect_uri = f"{str(self.base_url).rstrip('/')}{self._redirect_path}"
            token_params: dict[str, Any] = {
                "url": self._upstream_token_endpoint,
                "code": idp_code,
                "redirect_uri": idp_redirect_uri,
            }
            proxy_code_verifier = transaction.get("proxy_code_verifier")
            if proxy_code_verifier:
                token_params["code_verifier"] = proxy_code_verifier
            exchange_scopes = self._prepare_scopes_for_token_exchange(
                transaction.get("scopes") or []
            )
            if exchange_scopes:
                token_params["scope"] = " ".join(exchange_scopes)
            token_params.update(self._extra_token_params)

            try:
                async with self._upstream_oauth_client() as oauth_client:
                    idp_tokens: dict[str, Any] = await oauth_client.fetch_token(
                        **token_params
                    )
            except Exception as exchange_error:  # noqa: BLE001 - OAuth callback boundary
                logger.error(
                    "IdP token exchange failed due to %s",
                    type(exchange_error).__name__,
                )
                return self._error_response(
                    "Token exchange with identity provider failed.",
                    status_code=500,
                )

            raw_access_token = idp_tokens.get("access_token")
            if not isinstance(raw_access_token, str) or not raw_access_token:
                idp_tokens.clear()
                return self._error_response(
                    "Identity provider returned no usable access token.",
                    status_code=403,
                )

            proof: dict[str, Any] | None = None
            try:
                verified = await self._token_validator.verify_token(raw_access_token)
                proof = self._verified_identity_proof(verified)
            except Exception as verification_error:  # noqa: BLE001 - reject verifier failures
                logger.warning(
                    "GitHub identity verification failed due to %s",
                    type(verification_error).__name__,
                )
            finally:
                await self._cleanup_github_token(raw_access_token)
                idp_tokens.clear()

            if proof is None:
                return self._error_response(
                    "The authorized GitHub identity is not permitted.",
                    status_code=403,
                )

            client_code = secrets.token_urlsafe(32)
            code_expires_at = int(time.time() + DEFAULT_AUTH_CODE_EXPIRY_SECONDS)
            await self._code_store.put(
                key=client_code,
                value=ClientCode(
                    code=client_code,
                    client_id=transaction["client_id"],
                    redirect_uri=transaction["client_redirect_uri"],
                    code_challenge=transaction["code_challenge"],
                    code_challenge_method=transaction["code_challenge_method"],
                    scopes=transaction["scopes"],
                    idp_tokens=proof,
                    expires_at=code_expires_at,
                    created_at=time.time(),
                ),
                ttl=DEFAULT_AUTH_CODE_EXPIRY_SECONDS,
            )
            await self._transaction_store.delete(key=txn_id)

            client_redirect_uri = transaction["client_redirect_uri"]
            separator = "&" if "?" in client_redirect_uri else "?"
            callback_url = (
                f"{client_redirect_uri}{separator}"
                f"{urlencode({'code': client_code, 'state': transaction['client_state']})}"
            )
            response = RedirectResponse(url=callback_url, status_code=302)
            self._clear_consent_binding_cookie(request, response, txn_id)
            return response
        except Exception as callback_error:  # noqa: BLE001 - preserve callback error page
            logger.error(
                "OAuth callback processing failed due to %s",
                type(callback_error).__name__,
            )
            return self._error_response(
                "Internal server error during OAuth callback processing. Please try again.",
                status_code=500,
            )
