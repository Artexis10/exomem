"""Short-lived /upload tokens — for pasting a credential into a sandbox chat safely.

The claude.ai web code sandbox can reach the endpoint (once its egress allowlist
includes the host) and the uploaded files land on its disk — but it has no access
to the long-lived `EXOMEM_UPLOAD_TOKEN`. Pasting that secret into a chat transcript
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


#: Destination lanes an upload capability may be minted for. `evidence` signs the
#: bare `upload` scope so every token issued before lanes existed keeps verifying.
UPLOAD_LANES = ("evidence", "source")


def upload_scope(lane: str) -> str:
    """The signed scope for an upload capability bound to `lane`.

    The lane rides *inside* the signature rather than beside it, so the
    destination is fixed when the capability is minted and cannot be swapped by
    whoever posts the bytes. Without that, the out-of-band transport would
    reintroduce the defect this change exists to close: a client that cannot
    expose file handles would have its lane chosen by a form field.
    """
    if lane not in UPLOAD_LANES:
        raise ValueError(f"INVALID_MODE: upload lane must be one of {UPLOAD_LANES}")
    return "upload" if lane == "evidence" else f"upload:{lane}"


def lane_for_token(presented: str | None, secret: str, *, now: int | None = None) -> str | None:
    """The lane a presented upload token is bound to, or None if it verifies for none."""
    for lane in UPLOAD_LANES:
        if verify(presented, secret, scope=upload_scope(lane), now=now):
            return lane
    return None


def mint_for_endpoint(
    secret: str | None,
    base_url: str,
    *,
    scope: str = "upload",
    large_base_url: str | None = None,
    lane: str | None = None,
) -> dict:
    """Response payload for the `mint_<scope>_token` MCP tools (or raise if off).

    `scope` is "upload" or "download". Returns `{token, ttl_seconds, <scope>_url}`.
    Kept here, not inline in the tool closure, so it's unit-testable without the
    FastMCP machinery. Raising ValueError matches the tool→ValueError convention.

    When `large_base_url` is set (upload scope), also returns `large_upload_url` —
    an alternate endpoint (e.g. a Tailscale Funnel) NOT behind the ~100 MB
    Cloudflare edge cap, for uploads larger than that. The same minted token
    authenticates on both.
    """
    if secret is None:
        raise ValueError(f"{scope.upper()}_DISABLED: server has no EXOMEM_UPLOAD_TOKEN configured")
    # The lane is signed into the scope but never into the URL: both lanes post
    # to the same endpoint, and the server reads the destination off the token.
    signed_scope = upload_scope(lane) if lane is not None and scope == "upload" else scope
    out = {
        "token": mint(secret, scope=signed_scope),
        "ttl_seconds": DEFAULT_TTL,
        f"{scope}_url": f"{base_url}/{scope}",
    }
    if scope == "upload":
        out["lane"] = lane or "evidence"
    if large_base_url:
        out["large_upload_url"] = f"{large_base_url}/upload"
    return out
