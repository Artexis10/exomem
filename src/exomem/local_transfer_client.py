"""Invoke held-upload cancellation with the holding local client's existing identity."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .cli_ops import OpError


def cancel_uploads(
    arguments: dict[str, Any], *, authorization_carrier: Any,
    token_file: str = "", port: str = "", transport: Any = None,
) -> dict[str, Any]:
    import httpx

    from .governance.authorization_transport import AUTHORIZATION_SESSION_HEADER_NAME

    token_path = token_file or os.environ.get("EXOMEM_LOCAL_TOKEN_FILE", "").strip()
    raw_port = str(port or os.environ.get("EXOMEM_LOCAL_PORT", "")).strip()
    if not token_path or not raw_port.isascii() or not raw_port.isdigit() or not 0 < int(raw_port) < 65536:
        authorization_carrier.discard()
        raise OpError("LOCAL_UPLOAD_SESSION_REQUIRED", "set the holding client's local token file and local listener port")
    try:
        token = Path(token_path).expanduser().read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        token = ""
    if not token:
        authorization_carrier.discard()
        raise OpError("LOCAL_UPLOAD_SESSION_REQUIRED", "the holding client's local token file could not be read")
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
            response = client.post(f"http://127.0.0.1:{int(raw_port)}/api/transfer_artifact",
                                   headers=headers, json=arguments)
        reply = response.json()
    except (httpx.HTTPError, ValueError):
        raise OpError("LOCAL_UPLOAD_CANCELLATION_FAILED", "the local listener did not confirm cancellation; inspect or retry the same handles") from None
    if response.status_code != 200 or not isinstance(reply, dict) or reply.get("success") is not True:
        raise OpError("LOCAL_UPLOAD_CANCELLATION_FAILED", "the local listener refused cancellation; use the holding client's identity")
    data = reply.get("data")
    if not isinstance(data, dict):
        raise OpError("LOCAL_UPLOAD_CANCELLATION_FAILED", "the local listener returned no confirmed cancellation outcome")
    return data
