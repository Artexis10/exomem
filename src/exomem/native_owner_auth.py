"""Separate, short-lived browser identity for explicit native owner submissions."""

from __future__ import annotations

import hashlib
import re
import secrets
import time
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from key_value.aio.adapters.pydantic import PydanticAdapter
from pydantic import BaseModel, ConfigDict
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse
from starlette.routing import Route

from .governance.authorization_custody import standalone_attachment_id

if TYPE_CHECKING:
    from .session_oauth import ExomemSessionOAuthProxy

_TTL = 600
_STATE_PREFIX = "owner-login-"
_SESSION_PREFIX = "owner-session-"


class OwnerBrowserSession(BaseModel):
    """Identity and attachment only; this record grants no owner action."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    owner_id: str
    vault_root: str
    vault_binding: str
    session_id: str
    expires_at: float
    csrf_token: str


class _OwnerLogin(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    binding_digest: str
    vault_root: str
    vault_binding: str
    expires_at: float
    proxy_code_verifier: str


class NativeOwnerAuth:
    def __init__(
        self,
        proxy: ExomemSessionOAuthProxy,
        vault_root: Path,
        *,
        allow_insecure_loopback: bool = False,
    ) -> None:
        self._proxy = proxy
        origin = str(proxy.base_url).rstrip("/")
        parsed = urlsplit(origin)
        self._secure = parsed.scheme == "https"
        if (
            parsed.path
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
            or not parsed.hostname
            or not (
                self._secure
                or (
                    allow_insecure_loopback
                    and parsed.scheme == "http"
                    and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
                )
            )
        ):
            raise ValueError("Owner authentication requires a configured HTTPS origin")
        self.origin = origin
        self.vault_root = Path(vault_root).absolute()
        self._vault_binding = standalone_attachment_id(self.vault_root)
        prefix = "__Host-" if self._secure else ""
        self.binding_cookie_name = prefix + "exomem-owner-login"
        self.session_cookie_name = prefix + "exomem-owner-session"
        self._login_store = PydanticAdapter(
            key_value=proxy._client_storage,
            pydantic_model=_OwnerLogin,
            default_collection="exomem-owner-logins",
            raise_on_validation_error=True,
        )
        self._session_store = PydanticAdapter(
            key_value=proxy._client_storage,
            pydantic_model=OwnerBrowserSession,
            default_collection="exomem-owner-sessions",
            raise_on_validation_error=True,
        )

    def _expected_owner_id(self) -> str | None:
        from .server_auth import SingleUserGitHubVerifier

        verifier = self._proxy._token_validator
        if not isinstance(verifier, SingleUserGitHubVerifier):
            return None
        user_id = verifier._allowed_user_id
        if type(user_id) is not int or user_id <= 0:
            return None
        return f"github:{user_id}"

    def get_routes(self) -> list[Route]:
        return [Route("/owner/login", self.login, methods=["GET"])]

    def _binding_is_current(self, root: str, binding: str) -> bool:
        try:
            return (
                root == str(self.vault_root)
                and binding == self._vault_binding
                and standalone_attachment_id(self.vault_root) == binding
            )
        except (OSError, RuntimeError, ValueError):
            return False

    def _set_cookie(self, response: RedirectResponse, name: str, value: str) -> None:
        response.set_cookie(
            name, value, max_age=_TTL, path="/", secure=self._secure, httponly=True, samesite="lax"
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"

    async def login(self, request: Request) -> RedirectResponse:
        if not self._binding_is_current(str(self.vault_root), self._vault_binding):
            raise HTTPException(403, "Owner vault binding changed")
        state = _STATE_PREFIX + secrets.token_urlsafe(32)
        binding = secrets.token_urlsafe(32)
        transaction = _OwnerLogin(
            binding_digest=hashlib.sha256(binding.encode()).hexdigest(),
            vault_root=str(self.vault_root),
            vault_binding=self._vault_binding,
            expires_at=time.time() + _TTL,
            proxy_code_verifier=secrets.token_urlsafe(48),
        )
        await self._login_store.put(key=state, value=transaction, ttl=_TTL)
        response = RedirectResponse(
            self._proxy._build_upstream_authorize_url(state, transaction.model_dump()),
            status_code=302,
        )
        self._set_cookie(response, self.binding_cookie_name, binding)
        return response

    async def handle_callback(self, request: Request) -> HTMLResponse | RedirectResponse | None:
        state = request.query_params.get("state", "")
        if not state.startswith(_STATE_PREFIX):
            return None
        if not re.fullmatch(r"owner-login-[A-Za-z0-9_-]{43}", state):
            return self._proxy._error_response("Invalid owner login", status_code=400)
        transaction = await self._login_store.get(key=state)
        if transaction is None or not await self._login_store.delete(key=state):
            return self._proxy._error_response("Invalid or expired owner login", status_code=400)
        binding = request.cookies.get(self.binding_cookie_name, "")
        if (
            transaction.expires_at <= time.time()
            or not binding
            or not secrets.compare_digest(
                hashlib.sha256(binding.encode()).hexdigest(), transaction.binding_digest
            )
            or not self._binding_is_current(transaction.vault_root, transaction.vault_binding)
        ):
            return self._proxy._error_response("Owner login binding failed", status_code=403)
        code = request.query_params.get("code")
        if request.query_params.get("error") or not code:
            return self._proxy._error_response("Owner authentication failed", status_code=400)
        proof = await self._proxy._exchange_identity(code, transaction.model_dump())
        if isinstance(proof, HTMLResponse):
            return proof
        if transaction.expires_at <= time.time() or not self._binding_is_current(
            transaction.vault_root, transaction.vault_binding
        ):
            return self._proxy._error_response("Owner vault binding changed", status_code=403)
        owner_id = f"github:{proof['exomem_identity']['github_user_id']}"
        if owner_id != self._expected_owner_id():
            return self._proxy._error_response(
                "Owner identity configuration changed", status_code=403
            )
        session_id = _SESSION_PREFIX + secrets.token_urlsafe(32)
        session = OwnerBrowserSession(
            owner_id=owner_id,
            vault_root=transaction.vault_root,
            vault_binding=transaction.vault_binding,
            session_id=session_id,
            expires_at=time.time() + _TTL,
            csrf_token=secrets.token_urlsafe(32),
        )
        await self._session_store.put(key=session_id, value=session, ttl=_TTL)
        response = RedirectResponse("/owner", status_code=302)
        self._set_cookie(response, self.session_cookie_name, session_id)
        response.delete_cookie(
            self.binding_cookie_name, path="/", secure=self._secure, httponly=True, samesite="lax"
        )
        return response

    async def require_session(self, request: Request) -> OwnerBrowserSession:
        session_id = request.cookies.get(self.session_cookie_name, "")
        if not re.fullmatch(r"owner-session-[A-Za-z0-9_-]{43}", session_id):
            raise HTTPException(401, "Owner browser login required")
        session = await self._session_store.get(key=session_id)
        if (
            session is None
            or session.session_id != session_id
            or session.owner_id != self._expected_owner_id()
            or session.expires_at <= time.time()
            or not self._binding_is_current(session.vault_root, session.vault_binding)
        ):
            raise HTTPException(401, "Owner browser session expired or binding changed")
        return session

    async def require_submission(self, request: Request, csrf_token: str) -> OwnerBrowserSession:
        session = await self.require_session(request)
        if (
            request.method != "POST"
            or request.headers.getlist("origin") != [self.origin]
            or not isinstance(csrf_token, str)
            or not secrets.compare_digest(csrf_token.encode(), session.csrf_token.encode())
        ):
            raise HTTPException(403, "Owner submission origin or CSRF check failed")
        return session
