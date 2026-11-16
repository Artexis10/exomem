"""Invoke held-upload cancellation with the holding local client's existing identity."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .cli_ops import OpError


class LocalListenerUnset(ValueError):
    """The holding client's token file or listener port is not configured."""


class LocalListenerUnreadable(ValueError):
    """The configured token file gives no token."""


def local_listener(token_file: str = "", port: str = "") -> tuple[str, int]:
    """The holding local client's token and listener port, from flags or its environment."""
    token_path = token_file or os.environ.get("EXOMEM_LOCAL_TOKEN_FILE", "").strip()
    raw_port = str(port or os.environ.get("EXOMEM_LOCAL_PORT", "")).strip()
    if not token_path:
        raise LocalListenerUnset("a local client token file is required (--token-file or EXOMEM_LOCAL_TOKEN_FILE)")
    if not raw_port.isascii() or not raw_port.isdigit() or not 0 < int(raw_port) < 65536:
        raise LocalListenerUnset("a valid local port is required (--port or EXOMEM_LOCAL_PORT)")
    try:
        token = Path(token_path).expanduser().read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        raise LocalListenerUnreadable("the local client token file could not be read") from None
    if not token:
        raise LocalListenerUnreadable("the local client token file is empty")
    return token, int(raw_port)


def cancel_uploads(
    arguments: dict[str, Any], *, authorization_carrier: Any,
    token_file: str = "", port: str = "", transport: Any = None,
) -> dict[str, Any]:
    import httpx

    from .governance.authorization_transport import AUTHORIZATION_SESSION_HEADER_NAME

    try:
        token, listener_port = local_listener(token_file, port)
    except ValueError as error:
        authorization_carrier.discard()
        raise OpError("LOCAL_UPLOAD_SESSION_REQUIRED", str(error)) from None
    headers = {"Authorization": f"Bearer {token}"}
    if not authorization_carrier.is_absent:
        credential = authorization_carrier.consume()
        if not isinstance(credential, str):
            raise OpError("AUTHORIZATION_SESSION_UNAVAILABLE", "authorization session is unavailable")
        headers[AUTHORIZATION_SESSION_HEADER_NAME] = credential
    else:
        authorization_carrier.discard()
    try:
        with httpx.Client(transport=transport, trust_env=False, follow_redirects=False, timeout=10) as client:
            response = client.post(f"http://127.0.0.1:{listener_port}/api/transfer_artifact",
                                   headers=headers, json=arguments)
        reply = response.json()
    except (httpx.HTTPError, ValueError):
        raise OpError("LOCAL_UPLOAD_CANCELLATION_FAILED", "the local listener did not confirm cancellation; inspect or retry the same handles") from None
    if response.status_code != 200 or not isinstance(reply, dict) or reply.get("success") is not True:
        error = reply.get("error") if isinstance(reply, dict) else None
        if (isinstance(error, dict) and isinstance(error.get("code"), str)
                and isinstance(error.get("message"), str) and response.status_code not in (401, 403)):
            # The listener's own refusal (validation, busy) names its repair; relay it unchanged.
            raise OpError(error["code"], error["message"], error.get("remediation") or None)
        if response.status_code in (401, 403):
            raise OpError("LOCAL_UPLOAD_CANCELLATION_FAILED", "the local listener refused this identity; use the holding client's token file")
        raise OpError("LOCAL_UPLOAD_CANCELLATION_FAILED",
                      f"the local listener refused cancellation (HTTP {response.status_code}); retry the same handles")
    data = reply.get("data")
    if not isinstance(data, dict):
        raise OpError("LOCAL_UPLOAD_CANCELLATION_FAILED", "the local listener returned no confirmed cancellation outcome")
    return data
