"""Held uploads: a verified session's bytes, waiting for a command to commit them.

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
Each local session may keep at most `HOLD_MAX_COUNT` holds at once, holding
together no more bytes than the largest single hold accepts, so one hold of that
size always fits. A hold that would not fit in free disk is refused before a
byte is copied.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import shutil
import tempfile
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import BinaryIO

from filelock import FileLock
from filelock import Timeout as FileLockTimeout

from . import media_capacity, private_state
from .governance import principal

#: The `download_url` scheme of a held handle. Never fetched.
HELD_SCHEME = "exomem-held:"
#: How long a hold waits for its command.
HOLD_TTL_SECONDS = 3600
#: The lane of an archive that `imports.file` unpacks into a stage; no bearer-token route offers it.
IMPORT_LANE = "import"
#: Lanes a hold can be committed through, and the command that commits each.
LANE_COMMANDS = {
    "evidence": "preserve_artifacts", "source": "capture_source", IMPORT_LANE: "manage_memory imports.file",
}
STORE_DIRNAME = "held-uploads"
#: Live holds one local session may keep: two full file-handle batches.
HOLD_MAX_COUNT = 16

_SECRET = re.compile(r"[A-Za-z0-9_-]{43}")
_CHUNK = 1024 * 1024
UNAVAILABLE_REASON = "no redeemable held upload for this handle"
_now = time.time
# This suffix is the closed private-store protocol for committed cancellation.
_CANCELLING_SUFFIX = ".cancelling.json"


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


def principal_binding(who: principal.RequestPrincipal) -> str | None:
    """Bind an upload to a client, login generation and verified authorization context."""
    if (not who.resolved or not who.issuer_family
            or who.client_binding is None or who.origin_session is None):
        return None
    values = asdict(who)
    # These fields define authority across MCP and its delegated byte transport.
    fields = ("audience_id", "issuer_family", "client_binding", "origin_session",
              "session_id", "authorization_session_id", "verified_authorization_session")
    encoded = json.dumps({field: values[field] for field in fields}, sort_keys=True, separators=(",", ":"))
    return "mcp-session:" + hashlib.sha256(encoded.encode()).hexdigest()


def _bindings() -> tuple[str, ...]:
    """The verified session, plus its older local-grant binding when present."""
    bindings = []
    bound = principal_binding(principal.effective_principal())
    if bound is not None:
        bindings.append(bound)
    # Imported here: the tool schema reads this module's limits, and CLI prompt hooks must not load the MCP stack.
    from . import local_ingress

    grant = local_ingress.current_grant()
    if grant is not None and grant.session_id:
        bindings.append(f"mcp-local:{grant.session_id}")
    return tuple(bindings)


def _binding() -> str | None:
    bindings = _bindings()
    return bindings[0] if bindings else None


def _store(vault_root: Path) -> Path:
    return private_state.store(vault_root, STORE_DIRNAME)


def _key(secret: str) -> str:
    return hashlib.sha256(secret.encode("ascii")).hexdigest()


def _unavailable() -> HeldUploadError:
    return HeldUploadError("HELD_UPLOAD_UNAVAILABLE", UNAVAILABLE_REASON)


_remove = private_state.remove


def _finish_cancellation(store: Path, key: str, marker: Path) -> None:
    """Keep the cleanup owner and quota charge until payload removal is durable."""
    if (store / f"{key}.json").exists():
        raise OSError("conflicting upload ownership")
    private_state.sync_directory(store)
    (store / f"{key}.bin").unlink(missing_ok=True)
    private_state.sync_directory(store)
    marker.unlink()
    private_state.sync_directory(store)


def marker_key(marker: Path) -> str | None:
    """The storage key a cancellation marker names, or None for a file this store never wrote."""
    key = marker.name.removesuffix(_CANCELLING_SUFFIX)
    # Storage keys have the closed SHA-256 hex filename format; malformed state owns no other path.
    if len(key) == 64 and all(character in "0123456789abcdef" for character in key):
        return key
    return None


def _sweep(store: Path, now: float) -> None:
    """Drop expired holds and anything a crashed hold or claim left behind."""
    for meta_path in store.glob("*.json"):
        if meta_path.name.endswith(_CANCELLING_SUFFIX):
            key = marker_key(meta_path)
            if key is not None:
                try:
                    _finish_cancellation(store, key, meta_path)
                except OSError:
                    pass  # Its surviving marker retains the quota charge, including after expiry.
            continue
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


@contextmanager
def _hold_upload_room(vault_root: Path, *, device: int):
    from .cli_ops import OpError

    try:
        with media_capacity.upload_room(vault_root, device=device) as room:
            yield room
    except OpError as error:
        raise HeldUploadError(error.code, error.message) from error


def hold(
    vault_root: Path,
    stream: BinaryIO,
    *,
    lane: str,
    filename: str,
    content_type: str | None,
    max_bytes: int,
    expected_sha256: str | None = None,
    secret: str | None = None,
    import_key: str | None = None,
) -> dict:
    """Hold one upload for the verified session and return its file handle.

    A caller that can name the hold again without the reply passes its own `secret`; otherwise one is drawn.
    """
    binding = _binding()
    if binding is None:
        raise HeldUploadError(
            "HELD_UPLOAD_LOCAL_ONLY", "uploads need a verified client session"
        )
    if lane not in LANE_COMMANDS:
        raise HeldUploadError("INVALID_UPLOAD", f"lane must be one of {sorted(LANE_COMMANDS)}")
    name = Path(str(filename or "").replace("\\", "/")).name.strip()[:255]
    kind = (content_type or "").split(";", 1)[0].strip()[:255] or None

    store = _store(vault_root)
    with FileLock(store / ".lock"), _hold_upload_room(vault_root, device=store.stat().st_dev) as room:
        now = _now()
        _sweep(store, now)
        held_count, held_bytes = _usage(store, binding)
        if held_count >= HOLD_MAX_COUNT:
            raise _quota()
        # The route spools the body before it runs, so its length is known before a byte is copied.
        start = stream.tell()
        length = stream.seek(0, os.SEEK_END) - start
        stream.seek(start)
        if room is not None:
            room.require(length)
        if length > shutil.disk_usage(store).free:
            raise HeldUploadError("HELD_UPLOAD_NO_SPACE", "not enough free disk space to hold this upload")
        secret = secret if secret is not None else secrets.token_urlsafe(32)
        if _SECRET.fullmatch(secret) is None:
            raise HeldUploadError("INVALID_UPLOAD", "a hold secret is 43 URL-safe characters")
        key = _key(secret)
        if (store / f"{key}{_CANCELLING_SUFFIX}").exists():
            raise _unavailable()  # A committed cancellation still owns this payload identity.
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
                    # The session's held total is the largest single hold, so one such hold always fits.
                    if held_bytes + size > max_bytes:
                        raise _quota()
                    digest.update(chunk)
                    sink.write(chunk)
            if expected_sha256 is not None and not hmac.compare_digest(digest.hexdigest(), expected_sha256):
                raise HeldUploadError("SOURCE_HASH_MISMATCH", "upload bytes do not match expected_sha256")
            os.replace(part, store / f"{key}.bin")
            record = {
                "binding": binding,
                "lane": lane,
                "sha256": digest.hexdigest(),
                "size": size,
                "content_type": kind,
                "filename": name,
                "expires": now + HOLD_TTL_SECONDS,
                **({"import_key": import_key} if import_key is not None else {}),
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


def is_cancellable_reference(value: object) -> bool:
    """Whether a value has the held-upload handle form; it proves no ownership."""
    return is_held_reference(value) and _SECRET.fullmatch(value[len(HELD_SCHEME):]) is not None


def cancel(vault_root: Path, reference: str) -> int:
    """Cancel owned waiting bytes only after the launcher has settled older workers.

    The format activates only once this session's ownership is proven, so unknown,
    foreign and malformed handles never add a downgrade step.
    """
    from . import artifact_lifecycle, state_migration

    bindings = _bindings()
    if not bindings or not is_cancellable_reference(reference):
        raise _unavailable()
    key = _key(reference[len(HELD_SCHEME):])
    try:
        # A lock-free read: `_cancel` proves ownership again under the claim boundary.
        _owned(_store(vault_root), key, bindings)
    except OSError:
        raise _unavailable() from None
    try:
        artifact_lifecycle.activate_cancellation(vault_root)
    except TimeoutError:
        raise HeldUploadError("HELD_UPLOAD_BUSY", "upload ownership is busy; retry this handle") from None
    except state_migration.StateMigrationOfflineRequired:
        raise HeldUploadError(
            "HELD_UPLOAD_LIFECYCLE_REQUIRED",
            "update the managed launcher offline before cancelling held uploads",
        ) from None
    except OSError:
        raise HeldUploadError(
            "HELD_UPLOAD_CANCELLATION_INCOMPLETE", "upload cleanup could not be made durable; retry this handle",
        ) from None
    return _cancel(vault_root, reference)


def _owned(store: Path, key: str, bindings: tuple[str, ...]) -> tuple[Path, Path, int]:
    """Return the owning metadata, its marker path and charge, or raise unavailable.

    A surviving marker is owned work to finish; an active record must be unexpired with its payload.
    """
    active = store / f"{key}.json"
    marker = store / f"{key}{_CANCELLING_SUFFIX}"
    metadata = marker if marker.exists() else active
    try:
        record = json.loads(metadata.read_text(encoding="utf-8"))
        bound = str(record["binding"]).encode("utf-8")
        size = record["size"]
        expires = float(record["expires"])
    except (OSError, ValueError, KeyError, TypeError):
        raise _unavailable() from None
    if not any(hmac.compare_digest(bound, binding.encode("utf-8")) for binding in bindings):
        raise _unavailable()
    if type(size) is not int or size < 0 or not math.isfinite(expires) or expires <= 0:
        raise _unavailable()
    if metadata is active and (expires <= _now() or not (store / f"{key}.bin").is_file()):
        raise _unavailable()
    return metadata, marker, size


def _cancel(vault_root: Path, reference: str) -> int:
    """Cancel one owned unclaimed hold; the lifecycle-admitted leaf owns exposure.

    Return only its confirmed quota charge. Never inspect unrelated uploads or
    wait for the claim lock. Unavailable and failed operations claim no release.
    """
    bindings = _bindings()
    if not bindings or not is_cancellable_reference(reference):
        raise _unavailable()
    key = _key(reference[len(HELD_SCHEME):])
    try:
        store = _store(vault_root)
        with FileLock(store / ".lock", timeout=0):
            metadata, marker, size = _owned(store, key, bindings)
            if metadata != marker:
                # Windows requires a writable handle to flush the record; its contents stay unchanged.
                with metadata.open("r+b") as source:
                    os.fsync(source.fileno())
                os.rename(metadata, marker)
            _finish_cancellation(store, key, marker)
            return size
    except FileLockTimeout:
        raise HeldUploadError("HELD_UPLOAD_BUSY", "the upload claim is busy; retry shortly") from None
    except OSError:
        raise HeldUploadError(
            "HELD_UPLOAD_CANCELLATION_INCOMPLETE",
            "upload cancellation could not confirm cleanup; retry this handle",
        ) from None


def redeem(
    vault_root: Path,
    reference: str,
    *,
    lane: str | None,
    admit: Callable[[int], None] | None = None,
) -> Redeemed:
    """Claim one hold for the verified session through a command of `lane`.

    `admit` sees the held size before the claim, so a caller's byte budget can
    refuse a hold without spending it.
    """
    secret = reference[len(HELD_SCHEME) :] if is_held_reference(reference) else ""
    bindings = _bindings()
    if not bindings or _SECRET.fullmatch(secret) is None or lane is None:
        raise _unavailable()
    store = _store(vault_root)
    with FileLock(store / ".lock"):
        _sweep(store, _now())
        key = _key(secret)
        meta_path = store / f"{key}.json"
        try:
            record = json.loads(meta_path.read_text(encoding="utf-8"))
            bound = str(record["binding"])
            expires = float(record["expires"])
        except (OSError, ValueError, KeyError, TypeError):
            raise _unavailable() from None
        if not any(hmac.compare_digest(bound.encode("utf-8"), binding.encode("utf-8")) for binding in bindings):
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


def restore(vault_root: Path, redeemed: Redeemed) -> bool:
    """Put claimed bytes back as the same hold when their command stored nothing.

    The handle, lane, binding and expiry are unchanged, so the holding session
    can retry until the hold expires. Bytes that cannot be put back are removed.
    Returns whether the hold is back.
    """
    record = redeemed.record
    if not redeemed.key or record is None:
        _remove(redeemed.path)
        return False
    try:
        store = _store(vault_root)
        with FileLock(store / ".lock"):
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
        return False
    return True
