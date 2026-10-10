"""Held uploads: a local client's file bytes, waiting for a command to commit them.

`preserve-attachment-originals`. A same-machine client holds an attachment as a
local file, and no tool argument may name a local path. It sends the bytes to
the local listener's `/upload` with `hold=1` instead, and gets back a file
handle of the same four-field shape a chat client's attachment has. Its
`download_url` is an opaque `exomem-held:<secret>` reference, which
`preserve_artifacts` or `capture_source` redeems without a network request.

A hold is private machine-local state, never vault content. It is bound to the
local client session that sent it and to one lane, redeemable once, and gone
after `HOLD_TTL_SECONDS`. Every reason a handle cannot be redeemed by the
caller presenting it -- unknown, malformed, another session's, expired, spent
-- gets one answer, so a handle leaks nothing about holds its presenter does
not own. Checks run before the claim, so a refused redemption consumes nothing,
and a command that claims a hold but stores nothing puts it back with `restore`.
Each local session may keep at most `HOLD_MAX_COUNT` holds and `HOLD_MAX_BYTES`
held bytes at once.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from . import local_ingress, private_state

#: The `download_url` scheme of a held handle. Never fetched.
HELD_SCHEME = "exomem-held:"
#: How long a hold waits for its command.
HOLD_TTL_SECONDS = 3600
#: Lanes a hold can be committed through, and the command that commits each.
LANE_COMMANDS = {"evidence": "preserve_artifacts", "source": "capture_source"}
STORE_DIRNAME = "held-uploads"
#: Live holds one local session may keep: two full file-handle batches.
HOLD_MAX_COUNT = 16
#: Held bytes one local session may keep across its live holds.
HOLD_MAX_BYTES = 256 * 1024 * 1024

_SECRET = re.compile(r"[A-Za-z0-9_-]{43}")
_CHUNK = 1024 * 1024
UNAVAILABLE_REASON = "no redeemable held upload for this handle"
_now = time.time


class HeldUploadError(Exception):
    """A stable, content-free refusal to hold or redeem."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


@dataclass(frozen=True)
class Redeemed:
    """Held bytes claimed by one redemption; the caller owns and removes `path`."""

    path: Path
    size: int
    sha256: str
    content_type: str | None
    filename: str
    key: str = ""
    record: dict | None = None


def is_held_reference(value: object) -> bool:
    return isinstance(value, str) and value.startswith(HELD_SCHEME)


def _binding() -> str | None:
    """The verified local session of the live request, or None off local ingress."""
    grant = local_ingress.current_grant()
    if grant is None or not grant.session_id:
        return None
    return f"mcp-local:{grant.session_id}"


def _store(vault_root: Path) -> Path:
    return private_state.store(vault_root, STORE_DIRNAME)


def _key(secret: str) -> str:
    return hashlib.sha256(secret.encode("ascii")).hexdigest()


def _unavailable() -> HeldUploadError:
    return HeldUploadError("HELD_UPLOAD_UNAVAILABLE", UNAVAILABLE_REASON)


_remove = private_state.remove


def _sweep(store: Path, now: float) -> None:
    """Drop expired holds and anything a crashed hold or claim left behind."""
    for meta_path in store.glob("*.json"):
        try:
            expires = float(json.loads(meta_path.read_text(encoding="utf-8"))["expires"])
        except (OSError, ValueError, KeyError, TypeError):
            expires = 0.0
        if expires <= now:
            _remove(meta_path, meta_path.with_suffix(".bin"))
    for leftover in (*store.glob("*.part"), *store.glob("*.claimed-*")):
        try:
            stale = leftover.stat().st_mtime + 2 * HOLD_TTL_SECONDS <= now
        except OSError:
            continue
        if stale:
            _remove(leftover)


def _usage(store: Path, binding: str) -> tuple[int, int]:
    """Live holds and held bytes of one binding, after a sweep."""
    count = 0
    size = 0
    for meta_path in store.glob("*.json"):
        try:
            record = json.loads(meta_path.read_text(encoding="utf-8"))
            if not hmac.compare_digest(
                str(record["binding"]).encode("utf-8"), binding.encode("utf-8")
            ):
                continue
            size += int(record.get("size") or 0)
        except (OSError, ValueError, KeyError, TypeError):
            continue
        count += 1
    return count, size


def _quota() -> HeldUploadError:
    return HeldUploadError(
        "HELD_UPLOAD_QUOTA",
        "this session holds too many uploads; redeem or let some expire first",
    )


def _write_record(store: Path, key: str, record: dict) -> None:
    # The record is what makes a hold redeemable, so it lands last; `_sweep` owns its `.part` temp.
    private_state.write_record(store, key, record, temp_suffix=".part")


def hold(
    vault_root: Path,
    stream: BinaryIO,
    *,
    lane: str,
    filename: str,
    content_type: str | None,
    max_bytes: int,
) -> dict:
    """Hold one upload for the live local session and return its file handle."""
    binding = _binding()
    if binding is None:
        raise HeldUploadError(
            "HELD_UPLOAD_LOCAL_ONLY", "uploads are held only for a local client session"
        )
    if lane not in LANE_COMMANDS:
        raise HeldUploadError("INVALID_UPLOAD", f"lane must be one of {sorted(LANE_COMMANDS)}")
    name = Path(str(filename or "").replace("\\", "/")).name.strip()[:255]
    kind = (content_type or "").split(";", 1)[0].strip()[:255] or None

    store = _store(vault_root)
    now = _now()
    _sweep(store, now)
    held_count, held_bytes = _usage(store, binding)
    if held_count >= HOLD_MAX_COUNT:
        raise _quota()
    secret = secrets.token_urlsafe(32)
    key = _key(secret)
    fd, raw = tempfile.mkstemp(prefix=f"{key}.", suffix=".part", dir=store)
    part = Path(raw)
    digest = hashlib.sha256()
    size = 0
    try:
        with os.fdopen(fd, "wb") as sink:
            while chunk := stream.read(_CHUNK):
                size += len(chunk)
                if size > max_bytes:
                    raise HeldUploadError("TOO_LARGE", "upload exceeds the configured limit")
                if held_bytes + size > HOLD_MAX_BYTES:
                    raise _quota()
                digest.update(chunk)
                sink.write(chunk)
        os.replace(part, store / f"{key}.bin")
        record = {
            "binding": binding,
            "lane": lane,
            "sha256": digest.hexdigest(),
            "size": size,
            "content_type": kind,
            "filename": name,
            "expires": now + HOLD_TTL_SECONDS,
        }
        _write_record(store, key, record)
    except BaseException:
        _remove(part, store / f"{key}.bin")
        raise
    expires_at = dt.datetime.fromtimestamp(record["expires"], tz=dt.UTC)
    return {
        "state": "held",
        "lane": lane,
        "redeem_with": LANE_COMMANDS[lane],
        "expires_at": expires_at.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "size": size,
        "hash": record["sha256"],
        "hash_algorithm": "sha256",
        "content_type": kind,
        "file": {
            "download_url": HELD_SCHEME + secret,
            # Echoed into results and receipts, so it names the hold without
            # carrying the secret that redeems it.
            "file_id": f"held-{key[:16]}",
            "mime_type": kind or "application/octet-stream",
            "file_name": name or f"held-{key[:16]}",
        },
    }


def redeem(
    vault_root: Path,
    reference: str,
    *,
    lane: str | None,
    admit: Callable[[int], None] | None = None,
) -> Redeemed:
    """Claim one hold for the live local session through a command of `lane`.

    `admit` sees the held size before the claim, so a caller's byte budget can
    refuse a hold without spending it.
    """
    secret = reference[len(HELD_SCHEME) :] if is_held_reference(reference) else ""
    binding = _binding()
    if binding is None or _SECRET.fullmatch(secret) is None or lane is None:
        raise _unavailable()
    store = _store(vault_root)
    _sweep(store, _now())
    key = _key(secret)
    meta_path = store / f"{key}.json"
    try:
        record = json.loads(meta_path.read_text(encoding="utf-8"))
        bound = str(record["binding"])
        expires = float(record["expires"])
    except (OSError, ValueError, KeyError, TypeError):
        raise _unavailable() from None
    if not hmac.compare_digest(bound.encode("utf-8"), binding.encode("utf-8")):
        raise _unavailable()
    payload = store / f"{key}.bin"
    if expires <= _now():
        _remove(meta_path, payload)
        raise _unavailable()
    if record.get("lane") != lane:
        # Only the holding session gets here, so naming the lane discloses
        # nothing it does not already know.
        raise HeldUploadError(
            "HELD_UPLOAD_LANE",
            f"this upload is held for {LANE_COMMANDS.get(str(record.get('lane')), 'another lane')}",
        )
    if admit is not None:
        admit(int(record.get("size") or 0))
    claimed = store / f"{key}.claimed-{secrets.token_hex(8)}"
    try:
        os.rename(payload, claimed)  # exactly one redemption wins
    except OSError:
        raise _unavailable() from None
    _remove(meta_path)
    digest = hashlib.sha256()
    size = 0
    try:
        with claimed.open("rb") as source:
            while chunk := source.read(_CHUNK):
                size += len(chunk)
                digest.update(chunk)
    except OSError:
        _remove(claimed)
        raise _unavailable() from None
    if size != record.get("size") or digest.hexdigest() != record.get("sha256"):
        _remove(claimed)
        raise HeldUploadError(
            "HELD_UPLOAD_CHANGED", "the held bytes no longer match what was held"
        )
    return Redeemed(
        path=claimed,
        size=size,
        sha256=digest.hexdigest(),
        content_type=record.get("content_type") or None,
        filename=str(record.get("filename") or ""),
        key=key,
        record=record,
    )


def restore(vault_root: Path, redeemed: Redeemed) -> None:
    """Put claimed bytes back as the same hold when their command stored nothing.

    The handle, lane, binding and expiry are unchanged, so the holding session
    can retry until the hold expires. Bytes that cannot be put back are removed.
    """
    record = redeemed.record
    if not redeemed.key or record is None:
        _remove(redeemed.path)
        return
    try:
        store = _store(vault_root)
        if float(record["expires"]) <= _now():
            raise OSError("hold expired")
        payload = store / f"{redeemed.key}.bin"
        os.rename(redeemed.path, payload)
        try:
            _write_record(store, redeemed.key, record)
        except BaseException:
            _remove(payload)
            raise
    except (OSError, ValueError, KeyError, TypeError):
        _remove(redeemed.path)
