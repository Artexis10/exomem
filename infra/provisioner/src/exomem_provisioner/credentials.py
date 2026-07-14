"""Exact machine-credential contract shared with the hosted runtime."""

from __future__ import annotations

import base64
import binascii
import hmac


def validate_machine_credential(value: str) -> str:
    """Require canonical unpadded base64url for a non-degenerate 256-bit value."""

    if not isinstance(value, str) or len(value) != 43 or "=" in value:
        raise ValueError("cell credential must be canonical 256-bit base64url")
    try:
        decoded = base64.b64decode(value + "=", altchars=b"-_", validate=True)
    except (binascii.Error, ValueError) as error:
        raise ValueError("cell credential must be canonical 256-bit base64url") from error
    canonical = base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii")
    if len(decoded) != 32 or not hmac.compare_digest(canonical, value) or len(set(decoded)) < 8:
        raise ValueError("cell credential must be a non-degenerate 256-bit value")
    return value
