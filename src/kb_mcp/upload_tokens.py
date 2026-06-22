"""Short-lived /upload tokens — for pasting a credential into a sandbox chat safely.

The claude.ai web code sandbox can reach the endpoint (once its egress allowlist
includes the host) and the uploaded files land on its disk — but it has no access
to the long-lived `KB_MCP_UPLOAD_TOKEN`. Pasting that secret into a chat transcript
is the exposure we want to avoid.

So: mint a short-lived token, HMAC-signed with the long-lived secret, carrying only
an expiry. Paste THAT into the chat. `/upload` accepts it alongside the long-lived
token; if the transcript leaks, the minted token is dead within minutes and only ever
granted Evidence writes anyway. The long-lived secret never leaves the desk.

Format: `v1.<exp_unix>.<hmac_sha256_hex>`  (distinguishable from the 64-hex
long-lived token by the `v1.` prefix). Stateless — no server-side store; validity
is just "signature matches AND not past exp".
"""

from __future__ import annotations

import hashlib
import hmac
import time

PREFIX = "v1."
DEFAULT_TTL = 900  # 15 minutes


def _sig(secret: str, scope: str, exp: int) -> str:
    # The scope is part of the signed message, so an `upload` token can't be
    # replayed against `/download` (and vice-versa) even with the same secret.
    return hmac.new(secret.encode(), f"{scope}:{exp}".encode(), hashlib.sha256).hexdigest()


def mint(secret: str, *, scope: str = "upload", ttl: int = DEFAULT_TTL, now: int | None = None) -> str:
    """Return a short-lived token for `scope`, valid `ttl` seconds, signed with `secret`."""
    exp = int(now if now is not None else time.time()) + ttl
    return f"{PREFIX}{exp}.{_sig(secret, scope, exp)}"


def verify(presented: str | None, secret: str, *, scope: str = "upload", now: int | None = None) -> bool:
    """True iff `presented` is a well-formed, unexpired token for `scope` signed with `secret`."""
    if not presented or not presented.startswith(PREFIX):
        return False
    parts = presented.split(".")
    if len(parts) != 3:
        return False
    _, exp_str, sig = parts
    if not exp_str.isdigit():
        return False
    exp = int(exp_str)
    now_i = int(now if now is not None else time.time())
    if now_i > exp:
        return False
    return hmac.compare_digest(sig, _sig(secret, scope, exp))


def mint_for_endpoint(secret: str | None, base_url: str, *, scope: str = "upload") -> dict:
    """Response payload for the `mint_<scope>_token` MCP tools (or raise if off).

    `scope` is "upload" or "download". Returns `{token, ttl_seconds, <scope>_url}`.
    Kept here, not inline in the tool closure, so it's unit-testable without the
    FastMCP machinery. Raising ValueError matches the tool→ValueError convention.
    """
    if secret is None:
        raise ValueError(f"{scope.upper()}_DISABLED: server has no KB_MCP_UPLOAD_TOKEN configured")
    return {
        "token": mint(secret, scope=scope),
        "ttl_seconds": DEFAULT_TTL,
        f"{scope}_url": f"{base_url}/{scope}",
    }
