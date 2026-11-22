"""Single-use byte-upload tickets delegated by a verified MCP session."""

from __future__ import annotations

import asyncio
import base64
import contextvars
import hashlib
import json
import math
import os
import secrets
import shlex
import time
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from pathlib import Path
from urllib.parse import urlsplit

from filelock import FileLock

from . import (
    artifact_recovery,
    auth_sessions,
    cloud_cell,
    cloud_origin,
    held_uploads,
    private_state,
    upload_sessions,
    upload_tokens,
)
from .governance import principal, raw_protection

TOKEN_PREFIX = "exomem-upload:"
RESUMABLE_PREFIX = "exomem-upload-resumable:"
#: Lanes a ticket may name. The import lane is a ticket's alone: bearer tokens and `exomem attach` stay on the two lanes.
HANDOFF_LANES = (*upload_tokens.UPLOAD_LANES, held_uploads.IMPORT_LANE)
_TICKET_BYTES = 32
MAX_BYTES = 100 * 1024 * 1024
STORE_DIRNAME = "upload-handoffs"
# A downgraded worker can only look up a nonce in the legacy store.
_SOURCE_TICKET_STORE = "source-upload-handoffs"
UNAVAILABLE_REASON = "no usable upload handoff"
#: The upload route's content-free refusal for a ticket it cannot use: a new upload is always a valid way on.
TICKET_UNUSABLE_REASON = f"{UNAVAILABLE_REASON}; request a new one with upload_files"
#: A claimed ticket is spent whatever the send's outcome, so every refusal after the claim says so.
SPENT_REMEDY = "This upload is used now; a retry needs a new one from upload_files."
_now = time.time


class SourceOriginBusy(ValueError):
    """The issuing login could not be checked. Retry without replacing the ticket."""

    def __init__(self) -> None:
        super().__init__("UPLOAD_ORIGIN_BUSY: the issuing login cannot be checked yet")


class SourceTicketSpent(ValueError):
    """The service removed a ticket but could not confirm durable consumption."""


def _unavailable() -> ValueError:
    return ValueError("UPLOAD_HANDOFF_UNAVAILABLE: " + UNAVAILABLE_REASON)


def parse_ticket(value: object, *, resumable: bool = False) -> str | None:
    """Return the canonical nonce from an upload ticket, or None for any other shape."""
    prefix = RESUMABLE_PREFIX if resumable else TOKEN_PREFIX
    if not isinstance(value, str) or not value.startswith(prefix):
        return None
    nonce = value[len(prefix):]
    # The wire format fixes 256 bits as 64 lowercase hexadecimal characters.
    try:
        if len(nonce) == 2 * _TICKET_BYTES and bytes.fromhex(nonce).hex() == nonce:
            return nonce
    except ValueError:
        pass
    return None


def settle_consumption(vault_root: Path) -> None:
    """Flush deletions by excluded older workers before cancellation format activation."""
    store = private_state.store(vault_root, STORE_DIRNAME)
    with FileLock(store / ".lock", timeout=0):
        private_state.sync_directory(store)


def parse_handoff_token(value: object) -> str | None:
    """Return a handoff's upload token in either published form, or None.

    A personal cell issues its ticket directly. A Cloud cell wraps the same
    ticket in the gateway's routing envelope. Recognition grants no authority:
    the receiving endpoint verifies whichever form arrives.
    """
    if (parse_ticket(value) is not None or parse_ticket(value, resumable=True) is not None
            or cloud_origin.envelope_nonce(value) is not None):
        return value  # type: ignore[return-value]
    return None


def ticket_nonce(value: object) -> str | None:
    """The nonce of an upload token in either published form, or None; it names the upload without proving it."""
    return parse_ticket(value) or cloud_origin.envelope_nonce(value)


def hold_secret(nonce: str) -> str:
    """The secret of the hold that a ticket's upload becomes, derived so `imports.file` can name it from the token alone."""
    digest = hashlib.sha256(f"exomem-import-hold:{nonce}".encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


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


def _send_step(endpoint: str, lane: str, what: str, placeholder: str) -> dict:
    """The one send line of every ticket lane, with the token and the file left for the agent to fill in.

    The token stays out of the line: an egress scrubber rewrites a bearer value in reply text, and a
    rewritten header cannot pass the upload route. Double quotes let a shell variable stand for it.
    """
    return {
        "step": "send",
        "text": (f"Send {what} once, as multipart form fields. Replace <token> with this reply's `token` "
                 f"and {placeholder} with its path. A refused send also uses up this upload."),
        "shell": (f'curl -sS -H "Authorization: Bearer <token>" -F lane={lane} -F hold=1 '
                  f"-F 'file=@{placeholder}' {shlex.quote(endpoint)}"),
    }


def _import_steps(token: str, endpoint: str) -> list[dict]:
    """The two steps after an import ticket: send the archive, then repeat `imports.file` with the token."""
    return [
        _send_step(endpoint, held_uploads.IMPORT_LANE, "the tar or zip", "<archive>"),
        {"step": "import", "tool": "manage_memory",
         "arguments": {"operation": "imports.file", "arguments": {"upload": token}},
         "text": "Repeat this call until `filing` is `done`; a call that is cut short resumes where it stopped."},
    ]


def _file_steps(endpoint: str, lane: str) -> list[dict]:
    """The two steps after a source or evidence ticket: send the file once, then save the held handle."""
    from . import operation_entries

    # The public tool that carries the command which commits this lane's hold; it raises when none does.
    tool, _ = operation_entries.spell_call(held_uploads.LANE_COMMANDS[lane], {}, "public")
    return [
        _send_step(endpoint, lane, "the file", "<file>"),
        {"step": "save", "tool": tool,
         "text": "Pass the `file` that the send returns in `files`. Check each saved row's `hash` against "
                 "expected_sha256 before you tell the person it is saved."},
    ]


async def _active(root: Path, who: principal.RequestPrincipal) -> bool:
    # Audience alone identifies the owner across clients; it cannot identify a session.
    if (not who.resolved or who.surface != "mcp" or not who.issuer_family
            or who.client_binding is None or who.origin_session is None):
        return False
    if not raw_protection._authority(root, who)[0]:
        return False
    return await auth_sessions.origin_session_active(who)


def _require_source_login(state: auth_sessions.OriginLogin) -> None:
    if state is auth_sessions.OriginLogin.UNREADABLE:
        raise SourceOriginBusy
    if state is not auth_sessions.OriginLogin.VALID:
        raise _unavailable()


def _require_source_governance(root: Path, who: principal.RequestPrincipal) -> None:
    try:
        if who.surface == "mcp" and raw_protection.has_unrestricted_access(root, who, report_unreadable=True):
            return
    except raw_protection.AuthorityUnreadable:
        raise SourceOriginBusy from None
    raise _unavailable()


async def _check_source_authority(root: Path, who: principal.RequestPrincipal) -> None:
    _require_source_governance(root, who)
    _require_source_login(await auth_sessions.origin_login_state(who, require_owner=True))


def prepare_upload(
    vault_root: Path, *, lane: str, expected_sha256: str,
    base_url: str | None = None, max_bytes: int | None = None, import_identity: str | None = None,
    size_bytes: int | None = None, held_only: bool = False,
) -> dict:
    """Issue one upload handoff; the caller's current session owns its result."""
    async def prepare():
        from . import source_imports
        from .client_artifacts import fetch_budget
        from .source_capture import validate_sha256

        expected = validate_sha256(expected_sha256)
        import_key = None
        if import_identity is not None:
            if lane != "source":
                raise ValueError("INVALID_IMPORT_IDENTITY: use a held Source upload")
            import_key = source_imports.issuer_key(import_identity)
        if lane not in HANDOFF_LANES:
            raise ValueError(f"INVALID_MODE: upload lane must be one of {HANDOFF_LANES}")
        who = replace(principal.effective_principal(), administrative_ingress=False)
        cloud = cloud_cell.is_cloud_issuer_family(who.issuer_family)
        if cloud and lane == held_uploads.IMPORT_LANE:
            # A Cloud cell takes an import as an encrypted archive from `exomem cloud import`; this unpacker is not on that path.
            raise ValueError(
                "UPLOAD_LANE_UNAVAILABLE: lane import is for a personal server. "
                "A folder reaches Exomem Cloud with `exomem cloud import <folder>` on the person's computer")
        if who.client_binding is None or who.origin_session is None:
            if cloud:
                raise ValueError("UPLOAD_HANDOFF_UNAVAILABLE: this Exomem Cloud request has no verified gateway origin")
            raise ValueError("UPLOAD_HANDOFF_UNAVAILABLE: this transport has no verified HTTP session; use a personal HTTP MCP connection")
        configured = int(os.environ.get("EXOMEM_UPLOAD_MAX_BYTES", str(MAX_BYTES)))
        limit = min(configured, MAX_BYTES, fetch_budget().max_file_bytes)
        if max_bytes is not None:
            if type(max_bytes) is not int or max_bytes <= 0:
                raise ValueError("INVALID_UPLOAD_HANDOFF: max_bytes must be positive")
            limit = min(limit, max_bytes)
        if limit <= 0:
            raise _unavailable()
        if size_bytes is not None and (lane != "source" or type(size_bytes) is not int or size_bytes < 0):
            raise ValueError("INVALID_UPLOAD_HANDOFF: size_bytes must be a nonnegative Source size")
        large_source = size_bytes is not None and size_bytes > limit
        resumable = large_source and not (cloud or held_only or import_key is not None)
        if resumable:
            await _check_source_authority(vault_root, who)
        elif not await _active(vault_root, who):
            raise _unavailable()
        if size_bytes is not None and large_source:
            if not resumable:
                raise ValueError(f"TOO_LARGE: this handoff holds at most {limit:,} bytes")
            session_limit = upload_sessions.max_bytes_from_env()
            if size_bytes > session_limit:
                raise ValueError(f"TOO_LARGE: a Source session holds at most {session_limit:,} bytes")
            limit = size_bytes
        route = None
        if cloud:
            # The gateway's capsule names its own fixed endpoint and bounds the ticket's life.
            request_origin = cloud_origin.current()
            route = request_origin.route if request_origin is not None else None
            if route is None:
                raise _unavailable()
            endpoint = upload_endpoint(route.upload_url)
        else:
            base = (base_url if base_url is not None else os.environ.get("EXOMEM_BASE_URL", "")).rstrip("/")
            if not base:
                raise ValueError("UPLOAD_HANDOFF_UNAVAILABLE: this MCP session has no HTTP upload listener")
            endpoint = upload_endpoint(base + "/upload")
        binding = held_uploads.principal_binding(who)
        if binding is None:
            raise _unavailable()
        # Lowercase hex survives the terminal scrubber without reducing the ticket's 256-bit entropy.
        secret = secrets.token_hex(_TICKET_BYTES)
        if route is not None:
            token = cloud_origin.handoff_token(route, secret)
        else:
            token = (RESUMABLE_PREFIX if resumable else TOKEN_PREFIX) + secret
        # Name the next step before writing a ticket.
        if resumable:
            steps = [{"step": "send", "text": "Use exomem attach with this handoff file and the Source original. "
                      "The client resumes the upload and returns the verified Source receipt."}]
        elif lane == held_uploads.IMPORT_LANE:
            steps = _import_steps(token, endpoint)
        else:
            steps = _file_steps(endpoint, lane)
        store = private_state.store(vault_root, _SOURCE_TICKET_STORE if resumable else STORE_DIRNAME)
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
            ttl = upload_tokens.DEFAULT_TTL
            if route is not None:
                ttl = min(ttl, math.floor(route.expires - now))
                if ttl < 1:
                    raise _unavailable()
            expires = now + ttl
            key = hashlib.sha256(secret.encode("ascii")).hexdigest()
            private_state.write_record(store, key, {
                "principal": asdict(who), "binding": binding,
                "deployment": artifact_recovery.deployment(vault_root),
                "expires": expires, "lane": lane,
                "expected_sha256": expected, "max_bytes": limit,
                **({"import_key": import_key} if import_key is not None else {}),
                **({"size_bytes": size_bytes} if resumable else {}),
            }, temp_suffix=".part")
        return {
            "token": token, "upload_url": endpoint,
            "ttl_seconds": ttl, "hold": not resumable, "lane": lane,
            "expected_sha256": expected, "max_bytes": limit,
            "handoff_status": "handoff_prepared", "committed": False, "next": steps,
            **({"size_bytes": size_bytes, "resumable": upload_sessions.handoff(endpoint.removesuffix("/upload"), lane=lane)} if resumable else {}),
        }

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(prepare())
    # Synchronous command leaves can run on the ASGI loop; retain their verified request context.
    context = contextvars.copy_context()
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(context.run, lambda: asyncio.run(prepare())).result()


async def claim_upload(
    vault_root: Path, token: str, *, resumable: bool = False, size_bytes: int | None = None, sha256: str | None = None,
    admit: Callable[[principal.RequestPrincipal], Awaitable[None]] | None = None,
) -> dict:
    """Consume the ticket before byte receipt, after delegated admission if supplied."""
    secret = parse_ticket(token, resumable=resumable)
    if secret is None:
        raise _unavailable()
    store = private_state.store(vault_root, _SOURCE_TICKET_STORE if resumable else STORE_DIRNAME)
    key = hashlib.sha256(secret.encode("ascii")).hexdigest()
    path = store / f"{key}.json"
    with FileLock(store / ".lock"):
        try:
            from .source_capture import validate_sha256

            record = json.loads(path.read_text(encoding="utf-8"))
            who = upload_tokens._principal_from_values(dict(record["principal"]))
            validate_sha256(record["expected_sha256"])
            bound_size = record["max_bytes"]
            if resumable:
                if (record["lane"] != "source" or record["size_bytes"] != bound_size or size_bytes != bound_size
                        or sha256 != record["expected_sha256"] or not raw_protection.is_owner(who)):
                    raise _unavailable()
                maximum = upload_sessions.max_bytes_from_env()
            else:
                maximum = MAX_BYTES
                if "size_bytes" in record:
                    raise _unavailable()
            if (not math.isfinite(float(record["expires"])) or float(record["expires"]) <= _now()
                    or record["deployment"] != artifact_recovery.deployment(vault_root)
                    or record["binding"] != held_uploads.principal_binding(who)
                    or record["lane"] not in HANDOFF_LANES
                    or type(bound_size) is not int
                    or not 0 < bound_size <= maximum):
                raise _unavailable()
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            raise _unavailable() from None
    if resumable:
        await _check_source_authority(vault_root, who)
        if admit is not None:
            await admit(who)
    elif not await _active(vault_root, who):
        raise _unavailable()
    with FileLock(store / ".lock"):
        try:
            if json.loads(path.read_text(encoding="utf-8")) != record or float(record["expires"]) <= _now():
                raise _unavailable()
            path.unlink()
        except (OSError, ValueError, KeyError, TypeError):
            raise _unavailable() from None
        try:
            # Refuse before accepting bytes if consumption cannot survive a crash;
            # otherwise replay can recreate a cancelled import's upload identity.
            private_state.sync_directory(store)
        except OSError:
            if resumable:
                raise SourceTicketSpent(TICKET_UNUSABLE_REASON) from None
            raise _unavailable() from None
    return {**record, "principal": who, "nonce": secret,
            **({"source_handoff": {"principal": record["principal"], "binding": record["binding"],
                                  "deployment": record["deployment"], "ticket_sha256": key}} if resumable else {})}


def source_principal(vault_root: Path, record: dict) -> principal.RequestPrincipal:
    """Reconstruct a personal Source session's authority from its private, issued record."""
    try:
        from .source_capture import validate_sha256

        authority = record["source_handoff"]
        who = upload_tokens._principal_from_values(dict(authority["principal"]))
        validate_sha256(authority["ticket_sha256"])
        if (authority["deployment"] != artifact_recovery.deployment(vault_root)
                or not authority["binding"] or authority["binding"] != record["binding"]
                or authority["binding"] != held_uploads.principal_binding(who)):
            raise _unavailable()
    except (KeyError, TypeError, ValueError, AttributeError):
        raise _unavailable() from None
    _require_source_governance(vault_root, who)
    _require_source_login(auth_sessions.origin_login_state_sync(who, require_owner=True))
    return who
