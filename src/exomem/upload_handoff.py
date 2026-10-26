"""Single-use byte-upload tickets delegated by a verified MCP session."""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import math
import os
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from pathlib import Path
from urllib.parse import urlsplit

from filelock import FileLock

from . import artifact_recovery, auth_sessions, held_uploads, private_state, upload_tokens
from .governance import principal, raw_protection

TOKEN_PREFIX = "exomem-upload:"
_TICKET_BYTES = 32
MAX_BYTES = 100 * 1024 * 1024
STORE_DIRNAME = "upload-handoffs"
UNAVAILABLE_REASON = "no usable upload handoff"
_now = time.time


def _unavailable() -> ValueError:
    return ValueError("UPLOAD_HANDOFF_UNAVAILABLE: " + UNAVAILABLE_REASON)


def parse_ticket(value: object) -> str | None:
    """Return the canonical nonce from an upload ticket, or None for any other shape."""
    if not isinstance(value, str) or not value.startswith(TOKEN_PREFIX):
        return None
    nonce = value[len(TOKEN_PREFIX):]
    # The wire format fixes 256 bits as 64 lowercase hexadecimal characters.
    try:
        if len(nonce) == 2 * _TICKET_BYTES and bytes.fromhex(nonce).hex() == nonce:
            return nonce
    except ValueError:
        pass
    return None


def upload_endpoint(value: str) -> str:
    """Accept only the fixed upload path over HTTPS or literal loopback HTTP."""
    import ipaddress

    # Delimiters also reject an empty query or fragment, which urlsplit omits.
    if (not isinstance(value, str) or "?" in value or "#" in value
            or any(character.isspace() for character in value)):
        raise ValueError("INVALID_UPLOAD_HANDOFF: upload_url must name a secure /upload endpoint")
    try:
        parsed = urlsplit(value)
        loopback = parsed.hostname is not None and ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        loopback = False
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme == "https" or (parsed.scheme == "http" and loopback)
        ) and bool(parsed.hostname) and parsed.path == "/upload" and not (
            parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment
        )
        if not valid or (parsed.port is not None and not 0 < parsed.port < 65536):
            raise ValueError
    except ValueError:
        raise ValueError("INVALID_UPLOAD_HANDOFF: upload_url must name a secure /upload endpoint") from None
    return value


async def _active(root: Path, who: principal.RequestPrincipal) -> bool:
    # Audience alone identifies the owner across clients; it cannot identify a session.
    if (not who.resolved or who.surface != "mcp" or not who.issuer_family
            or who.client_binding is None or who.origin_session is None):
        return False
    if not raw_protection._authority(root, who)[0]:
        return False
    return await auth_sessions.origin_session_active(who)


def prepare_upload(
    vault_root: Path, *, lane: str, expected_sha256: str,
    base_url: str | None = None, max_bytes: int | None = None,
) -> dict:
    """Issue one held-byte handoff; the caller's current session owns its result."""
    async def prepare():
        from .client_artifacts import fetch_budget
        from .source_capture import validate_sha256

        expected = validate_sha256(expected_sha256)
        upload_tokens.upload_scope(lane)
        who = replace(principal.effective_principal(), administrative_ingress=False)
        if who.client_binding is None or who.origin_session is None:
            raise ValueError("UPLOAD_HANDOFF_UNAVAILABLE: this transport has no verified HTTP session; use a personal HTTP MCP connection")
        if not await _active(vault_root, who):
            raise _unavailable()
        base = (base_url if base_url is not None else os.environ.get("EXOMEM_BASE_URL", "")).rstrip("/")
        if not base:
            raise ValueError("UPLOAD_HANDOFF_UNAVAILABLE: this MCP session has no HTTP upload listener")
        endpoint = upload_endpoint(base + "/upload")
        configured = int(os.environ.get("EXOMEM_UPLOAD_MAX_BYTES", str(MAX_BYTES)))
        limit = min(configured, MAX_BYTES, fetch_budget().max_file_bytes)
        if max_bytes is not None:
            if type(max_bytes) is not int or max_bytes <= 0:
                raise ValueError("INVALID_UPLOAD_HANDOFF: max_bytes must be positive")
            limit = min(limit, max_bytes)
        if limit <= 0:
            raise _unavailable()
        binding = held_uploads.principal_binding(who)
        if binding is None:
            raise _unavailable()
        store = private_state.store(vault_root, STORE_DIRNAME)
        with FileLock(store / ".lock"):
            now = _now()
            count = 0
            for path in store.glob("*.json"):
                try:
                    record = json.loads(path.read_text(encoding="utf-8"))
                    if float(record["expires"]) <= now:
                        private_state.remove(path)
                    elif record.get("binding") == binding:
                        count += 1
                except (OSError, ValueError, KeyError, TypeError):
                    private_state.remove(path)
            if count >= held_uploads.HOLD_MAX_COUNT:
                raise ValueError("UPLOAD_HANDOFF_QUOTA: redeem or let previous handoffs expire")
            # Lowercase hex survives the terminal scrubber without reducing the ticket's 256-bit entropy.
            secret = secrets.token_hex(_TICKET_BYTES)
            key = hashlib.sha256(secret.encode("ascii")).hexdigest()
            private_state.write_record(store, key, {
                "principal": asdict(who), "binding": binding,
                "deployment": artifact_recovery.deployment(vault_root),
                "expires": now + upload_tokens.DEFAULT_TTL, "lane": lane,
                "expected_sha256": expected, "max_bytes": limit,
            }, temp_suffix=".part")
        return {
            "token": TOKEN_PREFIX + secret, "upload_url": endpoint,
            "ttl_seconds": upload_tokens.DEFAULT_TTL, "hold": True, "lane": lane,
            "expected_sha256": expected, "max_bytes": limit,
            "handoff_status": "handoff_prepared", "committed": False,
        }

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(prepare())
    # Synchronous command leaves can run on the ASGI loop; retain their verified request context.
    context = contextvars.copy_context()
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(context.run, lambda: asyncio.run(prepare())).result()


async def claim_upload(vault_root: Path, token: str) -> dict:
    """Consume a ticket before reading a request body, or give one content-free refusal."""
    secret = parse_ticket(token)
    if secret is None:
        raise _unavailable()
    store = private_state.store(vault_root, STORE_DIRNAME)
    key = hashlib.sha256(secret.encode("ascii")).hexdigest()
    path = store / f"{key}.json"
    with FileLock(store / ".lock"):
        try:
            from .source_capture import validate_sha256

            record = json.loads(path.read_text(encoding="utf-8"))
            who = upload_tokens._principal_from_values(dict(record["principal"]))
            validate_sha256(record["expected_sha256"])
            if (not math.isfinite(float(record["expires"])) or float(record["expires"]) <= _now()
                    or record["deployment"] != artifact_recovery.deployment(vault_root)
                    or record["binding"] != held_uploads.principal_binding(who)
                    or record["lane"] not in upload_tokens.UPLOAD_LANES
                    or type(record["max_bytes"]) is not int
                    or not 0 < record["max_bytes"] <= MAX_BYTES):
                raise _unavailable()
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            raise _unavailable() from None
    if not await _active(vault_root, who):
        raise _unavailable()
    with FileLock(store / ".lock"):
        try:
            if json.loads(path.read_text(encoding="utf-8")) != record or float(record["expires"]) <= _now():
                raise _unavailable()
            path.unlink()
        except (OSError, ValueError, KeyError, TypeError):
            raise _unavailable() from None
    return {**record, "principal": who}
