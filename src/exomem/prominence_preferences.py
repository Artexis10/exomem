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
#: Schema 1 held one identity-wide level. Schema 2 keeps that value and adds at
#: most one level per engagement context, under the SAME single revision — so a
#: stale write to one context cannot race a write to another, and `set` and
#: `clear` cannot interleave under one inspected revision.
_SCHEMA = 2
_LEGACY_SCHEMA = 1
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


def _unavailable() -> OpError:
    return OpError("PREFERENCE_STATE_UNAVAILABLE", "preference state is unavailable")


def _canonical_level(value: object) -> bool:
    """Whether a stored value is already the canonical spelling of a level."""
    return isinstance(value, str) and prominence.normalize(value) == value


def _valid_contexts(value: object) -> bool:
    return (
        isinstance(value, dict)
        and all(key in prominence.CONTEXTS for key in value)
        and all(_canonical_level(level) for level in value.values())
    )


def _read(path: Path) -> tuple[str | None, dict[str, str], str, str | None]:
    """The stored identity-wide level, per-context levels, revision and change id."""
    if not os.path.lexists(path):
        return None, {}, "missing", None
    if path.is_symlink() or not path.is_file():
        raise _unavailable()
    try:
        with path.open("rb") as stream:
            raw = stream.read(_MAX_BYTES + 1)
    except OSError as error:
        raise _unavailable() from error
    if len(raw) > _MAX_BYTES:
        raise _unavailable()
    try:
        record = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise _unavailable() from error
    if (
        not isinstance(record, dict)
        or type(record.get("schema")) is not int
        or not isinstance(record.get("change_id"), str)
    ):
        raise _unavailable()
    # Each schema pins its own exact key set. An unknown key, an unknown context
    # name, a non-canonical level and a record carrying no choice at all all fail
    # closed rather than resolving to a fabricated value.
    if record["schema"] == _LEGACY_SCHEMA:
        if set(record) != {"schema", "prominence", "change_id"} or not _canonical_level(
            record.get("prominence")
        ):
            raise _unavailable()
        stored, contexts = record["prominence"], {}
    elif record["schema"] == _SCHEMA:
        if (
            set(record) != {"schema", "prominence", "contexts", "change_id"}
            or not (record["prominence"] is None or _canonical_level(record["prominence"]))
            or not _valid_contexts(record["contexts"])
            or (record["prominence"] is None and not record["contexts"])
        ):
            raise _unavailable()
        stored = record["prominence"]
        contexts = {key: record["contexts"][key] for key in sorted(record["contexts"])}
    else:
        raise _unavailable()
    try:
        change_id = uuid.UUID(record["change_id"])
    except (ValueError, AttributeError):
        raise _unavailable() from None
    if change_id.version != 4 or str(change_id) != record["change_id"]:
        raise _unavailable()
    return stored, contexts, hashlib.sha256(raw).hexdigest(), record["change_id"]


def inspect(vault_root: Path) -> dict:
    """Inspect the current principal's saved preference without creating state."""
    audience = _identity()
    path = _preference_path(vault_root, audience)
    stored, contexts, revision, change_id = _read(path)
    return {
        "stored": stored,
        "contexts": contexts,
        "revision": revision,
        "receipt_id": change_id,
    }


def _write(path: Path, value: str | None, contexts: dict[str, str], change_id: str) -> None:
    """Write the record as schema 2 — the one-way upgrade happens here."""
    payload = json.dumps(
        {
            "schema": _SCHEMA,
            "prominence": value,
            "contexts": {key: contexts[key] for key in sorted(contexts)},
            "change_id": change_id,
        },
        separators=(",", ":"),
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


def _require_revision(expected_revision: object) -> None:
    if not isinstance(expected_revision, str):
        raise OpError("PREFERENCE_INVALID", "expected_revision must be a string")
    if _REVISION.fullmatch(expected_revision) is None:
        raise OpError("PREFERENCE_INVALID", "expected_revision has invalid syntax")


def _require_context(context: object) -> str:
    if not isinstance(context, str) or context not in prominence.CONTEXTS:
        raise OpError("PREFERENCE_INVALID", "unknown engagement context")
    return context


def _operator_override() -> str | None:
    return prominence.normalize(os.environ.get("EXOMEM_PROMINENCE"))


def _unchanged(stored: str | None, contexts: dict[str, str], revision: str, change_id: str | None):
    return {
        "stored": stored,
        "contexts": contexts,
        "revision": revision,
        "receipt_id": change_id,
        "mutated": False,
    }


def set_preference(
    vault_root: Path, value: str, expected_revision: str, context: str | None = None
) -> dict:
    """Set one principal's level using an exact revision compare-and-swap.

    Without a context this writes the identity-wide value, exactly as before. With
    one it writes only that context's value and leaves the identity-wide value
    alone. Either way the whole record moves to one new revision.
    """
    audience = _identity()
    canonical = prominence.normalize(value) if isinstance(value, str) else None
    if canonical is None:
        raise OpError("PREFERENCE_INVALID", "unknown prominence level")
    _require_revision(expected_revision)
    if context is not None:
        context = _require_context(context)
    override = _operator_override()
    if override is not None and canonical != override:
        raise OpError("PREFERENCE_OPERATOR_OVERRIDE", "operator prominence override is active")

    path = _preference_path(vault_root, audience, create=True)
    lock = FileLock(str(path.parent / ".lock"), timeout=_LOCK_TIMEOUT)
    try:
        with lock:
            stored, contexts, revision, change_id = _read(path)
            if revision != expected_revision:
                raise OpError("PREFERENCE_CONFLICT", "preference changed; inspect again")
            current = stored if context is None else contexts.get(context)
            if current == canonical:
                return _unchanged(stored, contexts, revision, change_id)
            if context is None:
                stored = canonical
            else:
                contexts = {**contexts, context: canonical}
            new_change_id = str(uuid.uuid4())
            _write(path, stored, contexts, new_change_id)
            _, _, new_revision, _ = _read(path)
            return {
                "stored": stored,
                "contexts": contexts,
                "revision": new_revision,
                "receipt_id": new_change_id,
                "mutated": True,
            }
    except Timeout as error:
        raise _unavailable() from error


def clear_preference(vault_root: Path, context: str, expected_revision: str) -> dict:
    """Remove one context's saved level, leaving the identity-wide value alone.

    Clearing a context that holds nothing is a no-op that reports no mutation and
    writes nothing. An operator override is refused before any write, as for set:
    the pinned level would keep applying, so reporting a change would be a lie.
    """
    audience = _identity()
    context = _require_context(context)
    _require_revision(expected_revision)
    if _operator_override() is not None:
        raise OpError("PREFERENCE_OPERATOR_OVERRIDE", "operator prominence override is active")

    path = _preference_path(vault_root, audience)
    if not os.path.lexists(path):
        stored, contexts, revision, change_id = _read(path)
        if revision != expected_revision:
            raise OpError("PREFERENCE_CONFLICT", "preference changed; inspect again")
        return _unchanged(stored, contexts, revision, change_id)

    lock = FileLock(str(path.parent / ".lock"), timeout=_LOCK_TIMEOUT)
    try:
        with lock:
            stored, contexts, revision, change_id = _read(path)
            if revision != expected_revision:
                raise OpError("PREFERENCE_CONFLICT", "preference changed; inspect again")
            if context not in contexts:
                return _unchanged(stored, contexts, revision, change_id)
            contexts = {key: level for key, level in contexts.items() if key != context}
            if stored is None and not contexts:
                # A record holding no choice at all IS the absent record: keeping
                # an empty one would write the shape `_read` fails closed on.
                os.unlink(path)
                return {
                    "stored": None,
                    "contexts": {},
                    "revision": "missing",
                    "receipt_id": None,
                    "mutated": True,
                }
            new_change_id = str(uuid.uuid4())
            _write(path, stored, contexts, new_change_id)
            _, _, new_revision, _ = _read(path)
            return {
                "stored": stored,
                "contexts": contexts,
                "revision": new_revision,
                "receipt_id": new_change_id,
                "mutated": True,
            }
    except Timeout as error:
        raise _unavailable() from error
