"""Identity-scoped prominence preferences."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import uuid
from pathlib import Path

from filelock import FileLock, Timeout

from . import prominence, state_paths
from .cli_ops import OpError
from .governance import principal as principal_module

_DIRECTORY = "prominence-preferences"
_SCHEMA = 1
_MAX_BYTES = 64 * 1024
_LOCK_TIMEOUT = 5
_REVISION = re.compile(r"(?:missing|[0-9a-f]{64})\Z")


def _identity() -> str:
    current = principal_module.effective_principal()
    audience = str(current.audience_id or "").strip()
    if not current.resolved or not audience or audience.startswith("\x00"):
        raise OpError("PREFERENCE_IDENTITY_REQUIRED", "a verified principal is required")
    return audience


def _preference_path(vault_root: Path, audience: str, *, create: bool = False) -> Path:
    state_root = (
        state_paths.ensure_vault_state_dir(vault_root)
        if create
        else state_paths.vault_state_dir(vault_root)
    )
    directory = state_root / _DIRECTORY
    if os.path.lexists(directory) and (directory.is_symlink() or not directory.is_dir()):
        raise OpError("PREFERENCE_STATE_UNAVAILABLE", "preference state is unavailable")
    if create:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, stat.S_IRWXU)
    digest = hashlib.sha256(audience.encode("utf-8")).hexdigest()
    return directory / f"{digest}.json"


def _read(path: Path) -> tuple[str | None, str, str | None]:
    if not os.path.lexists(path):
        return None, "missing", None
    if path.is_symlink() or not path.is_file():
        raise OpError("PREFERENCE_STATE_UNAVAILABLE", "preference state is unavailable")
    try:
        with path.open("rb") as stream:
            raw = stream.read(_MAX_BYTES + 1)
    except OSError as error:
        raise OpError("PREFERENCE_STATE_UNAVAILABLE", "preference state is unavailable") from error
    if len(raw) > _MAX_BYTES:
        raise OpError("PREFERENCE_STATE_UNAVAILABLE", "preference state is unavailable")
    try:
        record = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise OpError("PREFERENCE_STATE_UNAVAILABLE", "preference state is unavailable") from error
    if (
        not isinstance(record, dict)
        or set(record) != {"schema", "prominence", "change_id"}
        or type(record.get("schema")) is not int
        or record.get("schema") != _SCHEMA
        or not isinstance(record.get("prominence"), str)
        or prominence.normalize(record["prominence"]) != record["prominence"]
        or not isinstance(record.get("change_id"), str)
    ):
        raise OpError("PREFERENCE_STATE_UNAVAILABLE", "preference state is unavailable")
    try:
        change_id = uuid.UUID(record["change_id"])
    except (ValueError, AttributeError):
        raise OpError("PREFERENCE_STATE_UNAVAILABLE", "preference state is unavailable") from None
    if change_id.version != 4 or str(change_id) != record["change_id"]:
        raise OpError("PREFERENCE_STATE_UNAVAILABLE", "preference state is unavailable")
    return record["prominence"], hashlib.sha256(raw).hexdigest(), record["change_id"]


def inspect(vault_root: Path) -> dict:
    """Inspect the current principal's saved preference without creating state."""
    audience = _identity()
    path = _preference_path(vault_root, audience)
    stored, revision, change_id = _read(path)
    return {"stored": stored, "revision": revision, "receipt_id": change_id}


def _write(path: Path, value: str, change_id: str) -> None:
    payload = json.dumps(
        {"schema": _SCHEMA, "prominence": value, "change_id": change_id}, separators=(",", ":")
    )
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def set_preference(vault_root: Path, value: str, expected_revision: str) -> dict:
    """Set one principal's preference using an exact revision compare-and-swap."""
    audience = _identity()
    canonical = prominence.normalize(value) if isinstance(value, str) else None
    if canonical is None:
        raise OpError("PREFERENCE_INVALID", "unknown prominence level")
    if not isinstance(expected_revision, str):
        raise OpError("PREFERENCE_INVALID", "expected_revision must be a string")
    if _REVISION.fullmatch(expected_revision) is None:
        raise OpError("PREFERENCE_INVALID", "expected_revision has invalid syntax")
    override = prominence.normalize(os.environ.get("EXOMEM_PROMINENCE"))
    if override is not None and canonical != override:
        raise OpError("PREFERENCE_OPERATOR_OVERRIDE", "operator prominence override is active")

    path = _preference_path(vault_root, audience, create=True)
    lock = FileLock(str(path.parent / ".lock"), timeout=_LOCK_TIMEOUT)
    try:
        with lock:
            stored, revision, change_id = _read(path)
            if revision != expected_revision:
                raise OpError("PREFERENCE_CONFLICT", "preference changed; inspect again")
            if stored == canonical:
                return {
                    "stored": stored,
                    "revision": revision,
                    "receipt_id": change_id,
                    "mutated": False,
                }
            new_change_id = str(uuid.uuid4())
            _write(path, canonical, new_change_id)
            _, new_revision, _ = _read(path)
            return {
                "stored": canonical,
                "revision": new_revision,
                "receipt_id": new_change_id,
                "mutated": True,
            }
    except Timeout as error:
        raise OpError("PREFERENCE_STATE_UNAVAILABLE", "preference state is unavailable") from error
