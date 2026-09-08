"""Durable external ledger for authenticated native owner reviews."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from . import mutation_lock, vocabulary_placement
from .governance import authorization_custody
from .governance.authorization_custody import standalone_attachment_id

MAX_REVIEW_JSON_BYTES = 12 * 1024 * 1024
MAX_REVIEW_EXPIRY_SECONDS = 300

_SCHEMA_VERSION = "1"
_ACTIONS = frozenset({"policy", "migration", "activation", "approve", "deny", "grant", "revoke"})
_STATES = frozenset({"prepared", "accepted", "applying", "completed"})
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_REVIEW_ID = re.compile(r"owner-review-[0-9a-f]{32}\Z")
_COLUMNS = (
    "review_id",
    "vault_binding",
    "owner_id",
    "action",
    "body_json",
    "display_json",
    "binding_digest",
    "expires_at",
    "started_at",
    "state",
    "result_json",
)


class NativeOwnerReviewUnavailable(RuntimeError):
    """The durable ledger or its attachment cannot be trusted."""


class NativeOwnerReviewDenied(RuntimeError):
    """The caller does not own a current review transition."""


class NativeOwnerReviewConflict(RuntimeError):
    """The requested review transition is no longer current."""


def _text(value: object, *, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > maximum:
        raise ValueError("OWNER_REVIEW_INVALID")
    return value


def _timestamp(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= (1 << 63) - 1:
        raise ValueError("OWNER_REVIEW_INVALID")
    return value


def _binding(value: object) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError("OWNER_REVIEW_INVALID")
    return value


def _json_value(value: object) -> None:
    if value is None or type(value) in {str, bool, int, float}:
        if isinstance(value, float) and not (-float("inf") < value < float("inf")):
            raise ValueError("OWNER_REVIEW_INVALID")
        return
    if isinstance(value, list):
        for item in value:
            _json_value(item)
        return
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        for item in value.values():
            _json_value(item)
        return
    raise ValueError("OWNER_REVIEW_INVALID")


def _encode_object(value: object) -> str:
    if not isinstance(value, dict):
        raise ValueError("OWNER_REVIEW_INVALID")
    _json_value(value)
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError, RecursionError, UnicodeError):
        raise ValueError("OWNER_REVIEW_INVALID") from None


def _decode_object(value: object) -> dict[str, Any]:
    if not isinstance(value, str):
        raise NativeOwnerReviewUnavailable
    try:
        decoded = json.loads(value)
        if not isinstance(decoded, dict) or _encode_object(decoded) != value:
            raise NativeOwnerReviewUnavailable
        return decoded
    except (json.JSONDecodeError, TypeError, ValueError, RecursionError):
        raise NativeOwnerReviewUnavailable from None


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class OwnerReview:
    review_id: str
    vault_binding: str
    owner_id: str
    action: str
    body: Mapping[str, Any]
    display: Mapping[str, Any]
    binding_digest: str
    expires_at: int
    started_at: int | None
    state: str
    result: Mapping[str, Any] | None
    expired: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "review_id": self.review_id,
            "vault_binding": self.vault_binding,
            "owner_id": self.owner_id,
            "action": self.action,
            "body": _thaw(self.body),
            "display": _thaw(self.display),
            "binding_digest": self.binding_digest,
            "expires_at": self.expires_at,
            "started_at": self.started_at,
            "state": self.state,
            "result": None if self.result is None else _thaw(self.result),
            "expired": self.expired,
        }


class OwnerReviewStore:
    """One attachment-bound external review ledger."""

    def __init__(self, vault_root: Path) -> None:
        self.vault_root = Path(vault_root).absolute()
        try:
            self._vault_binding = standalone_attachment_id(self.vault_root)
            self._directory = vocabulary_placement.validated_authority_directory(
                self.vault_root
            )
        except Exception as exc:  # noqa: BLE001 - construction is a fail-closed boundary
            raise NativeOwnerReviewUnavailable from exc
        token = hashlib.sha256(self._vault_binding.encode("utf-8")).hexdigest()
        self.database_path = self._directory / f"owner-reviews-{token}.sqlite"

    def _recheck_attachment(self) -> None:
        try:
            if (
                standalone_attachment_id(self.vault_root) != self._vault_binding
                or vocabulary_placement.validated_authority_directory(self.vault_root)
                != self._directory
            ):
                raise NativeOwnerReviewUnavailable
        except NativeOwnerReviewUnavailable:
            raise
        except Exception:  # noqa: BLE001 - attachment parsing is fail-closed
            raise NativeOwnerReviewUnavailable from None

    @staticmethod
    def _validate_sidecars(path: Path) -> None:
        from .vocabulary_authority import VocabularyAuthority

        try:
            VocabularyAuthority._validate_sqlite_sidecars(path)  # noqa: SLF001
        except Exception:  # noqa: BLE001 - translate the shared fail-closed guard
            raise NativeOwnerReviewUnavailable from None

    def _connect(self, *, create: bool) -> sqlite3.Connection:
        self._recheck_attachment()
        path = self.database_path
        if not authorization_custody._private_parent_is_safe(path.parent):  # noqa: SLF001
            raise NativeOwnerReviewUnavailable
        connection: sqlite3.Connection | None = None
        try:
            exists = os.path.lexists(path)
            if not exists and not create:
                raise NativeOwnerReviewUnavailable
            if not exists:
                from .vocabulary_authority import (
                    VocabularyAuthorityUnavailable,
                    _publish_new_private_file,
                )

                try:
                    _publish_new_private_file(path, b"")
                except VocabularyAuthorityUnavailable:
                    raise NativeOwnerReviewUnavailable from None
            self._validate_sidecars(path)
            retained = mutation_lock.retain_regular_file(path, delete_access=False)
            try:
                info = os.fstat(retained.fd)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_nlink != 1
                    or not authorization_custody._file_is_owner_protected(  # noqa: SLF001
                        retained.fd, info
                    )
                ):
                    raise NativeOwnerReviewUnavailable
                connection = sqlite3.connect(path, timeout=5, isolation_level=None)
                connection.execute("PRAGMA journal_mode = DELETE")
                connection.execute("PRAGMA synchronous = FULL")
                connection.execute("PRAGMA foreign_keys = ON")
                self._ensure_schema(connection, create=create)
                self._validate_sidecars(path)
                if not mutation_lock._same_file_entry(  # noqa: SLF001
                    retained.directory, path.name, retained.fd
                ):
                    raise NativeOwnerReviewUnavailable
            finally:
                retained.close()
            return connection
        except NativeOwnerReviewUnavailable:
            if connection is not None:
                connection.close()
            raise
        except (OSError, sqlite3.Error, ValueError):
            if connection is not None:
                connection.close()
            raise NativeOwnerReviewUnavailable from None

    def _ensure_schema(self, connection: sqlite3.Connection, *, create: bool) -> None:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if not tables:
            if not create:
                raise NativeOwnerReviewUnavailable
            connection.executescript(
                """
                CREATE TABLE metadata (
                    name TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE owner_reviews (
                    review_id TEXT PRIMARY KEY,
                    vault_binding TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    body_json TEXT NOT NULL,
                    display_json TEXT NOT NULL,
                    binding_digest TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    started_at INTEGER,
                    state TEXT NOT NULL,
                    result_json TEXT
                );
                CREATE TABLE settings (
                    name TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            connection.executemany(
                "INSERT INTO metadata(name, value) VALUES (?, ?)",
                (
                    ("schema_version", _SCHEMA_VERSION),
                    ("vault_binding", self._vault_binding),
                ),
            )
            return
        if tables != {"metadata", "owner_reviews", "settings"}:
            raise NativeOwnerReviewUnavailable
        extras = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE "
            "type IN ('view', 'trigger') OR (type = 'index' AND sql IS NOT NULL) LIMIT 1"
        ).fetchone()
        columns = tuple(
            str(row[1]) for row in connection.execute("PRAGMA table_info(owner_reviews)")
        )
        metadata = dict(connection.execute("SELECT name, value FROM metadata"))
        settings = connection.execute("SELECT name FROM settings").fetchall()
        if (
            extras is not None
            or columns != _COLUMNS
            or metadata
            != {"schema_version": _SCHEMA_VERSION, "vault_binding": self._vault_binding}
            or any(row != ("renewal_review_id",) for row in settings)
        ):
            raise NativeOwnerReviewUnavailable

    def _row(self, connection: sqlite3.Connection, review_id: str) -> tuple[Any, ...]:
        review = _text(review_id)
        if _REVIEW_ID.fullmatch(review) is None:
            raise NativeOwnerReviewDenied
        row = connection.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM owner_reviews WHERE review_id = ?",
            (review,),
        ).fetchone()
        if row is None:
            raise NativeOwnerReviewDenied
        return tuple(row)

    def _review(self, row: tuple[Any, ...], *, owner_id: str, now: int) -> OwnerReview:
        if len(row) != len(_COLUMNS):
            raise NativeOwnerReviewUnavailable
        (
            review_id,
            vault_binding,
            stored_owner,
            action,
            body_json,
            display_json,
            binding_digest,
            expires_at,
            started_at,
            state,
            result_json,
        ) = row
        try:
            if (
                not isinstance(review_id, str)
                or _REVIEW_ID.fullmatch(review_id) is None
                or vault_binding != self._vault_binding
                or action not in _ACTIONS
                or state not in _STATES
            ):
                raise NativeOwnerReviewUnavailable
            stored_owner = _text(stored_owner)
            binding_digest = _binding(binding_digest)
            expires_at = _timestamp(expires_at)
            if started_at is not None:
                started_at = _timestamp(started_at)
            if (
                (state in {"prepared", "accepted"} and started_at is not None)
                or (state in {"applying", "completed"} and started_at is None)
                or (started_at is not None and started_at >= expires_at)
            ):
                raise NativeOwnerReviewUnavailable
            body = _decode_object(body_json)
            display = _decode_object(display_json)
            result = None if result_json is None else _decode_object(result_json)
            encoded_size = sum(
                len(value.encode("utf-8"))
                for value in (body_json, display_json, result_json)
                if value is not None
            )
            if encoded_size > MAX_REVIEW_JSON_BYTES:
                raise NativeOwnerReviewUnavailable
        except NativeOwnerReviewUnavailable:
            raise
        except (TypeError, ValueError, UnicodeError):
            raise NativeOwnerReviewUnavailable from None
        if stored_owner != _text(owner_id):
            raise NativeOwnerReviewDenied
        current = _timestamp(now)
        return OwnerReview(
            review_id,
            vault_binding,
            stored_owner,
            action,
            _freeze(body),
            _freeze(display),
            binding_digest,
            expires_at,
            started_at,
            state,
            None if result is None else _freeze(result),
            expires_at <= current,
        )

    def prepare(
        self,
        *,
        owner_id: str,
        action: str,
        body: dict[str, Any],
        display: dict[str, Any],
        binding_digest: str,
        expires_at: int,
        now: int,
    ) -> OwnerReview:
        owner = _text(owner_id)
        if not isinstance(action, str) or action not in _ACTIONS:
            raise ValueError("OWNER_REVIEW_INVALID")
        binding = _binding(binding_digest)
        current = _timestamp(now)
        expiry = _timestamp(expires_at)
        if expiry <= current or expiry > current + MAX_REVIEW_EXPIRY_SECONDS:
            raise NativeOwnerReviewDenied
        body_json = _encode_object(body)
        display_json = _encode_object(display)
        if len(body_json.encode("utf-8")) + len(display_json.encode("utf-8")) > MAX_REVIEW_JSON_BYTES:
            raise ValueError("OWNER_REVIEW_TOO_LARGE")
        review_id = f"owner-review-{uuid.uuid4().hex}"
        connection = self._connect(create=True)
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._recheck_attachment()
            connection.execute(
                "INSERT INTO owner_reviews VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, NULL, 'prepared', NULL)",
                (
                    review_id,
                    self._vault_binding,
                    owner,
                    action,
                    body_json,
                    display_json,
                    binding,
                    expiry,
                ),
            )
            self._validate_sidecars(self.database_path)
            connection.commit()
            return self._review(
                self._row(connection, review_id), owner_id=owner, now=current
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get(self, review_id: str, *, owner_id: str, now: int) -> OwnerReview:
        connection = self._connect(create=False)
        try:
            return self._review(
                self._row(connection, review_id), owner_id=owner_id, now=now
            )
        finally:
            connection.close()

    def _transition(
        self,
        review_id: str,
        *,
        owner_id: str,
        now: int,
        before: str,
        after: str,
        require_live: bool,
    ) -> OwnerReview:
        current = _timestamp(now)
        connection = self._connect(create=False)
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._recheck_attachment()
            review = self._review(
                self._row(connection, review_id), owner_id=owner_id, now=current
            )
            if review.state != before:
                raise NativeOwnerReviewConflict
            if require_live and review.expired:
                raise NativeOwnerReviewDenied
            if after == "applying":
                updated = connection.execute(
                    "UPDATE owner_reviews SET state = ?, started_at = ? "
                    "WHERE review_id = ? AND state = ? AND started_at IS NULL",
                    (after, current, review.review_id, before),
                )
            elif after == "accepted":
                updated = connection.execute(
                    "UPDATE owner_reviews SET state = ?, started_at = NULL "
                    "WHERE review_id = ? AND state = ? AND started_at IS NOT NULL",
                    (after, review.review_id, before),
                )
            else:
                raise NativeOwnerReviewConflict
            if updated.rowcount != 1:
                raise NativeOwnerReviewConflict
            self._validate_sidecars(self.database_path)
            connection.commit()
            return self._review(
                self._row(connection, review.review_id), owner_id=owner_id, now=current
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def accept(
        self,
        review_id: str,
        *,
        owner_id: str,
        expected_binding: str,
        now: int,
    ) -> OwnerReview:
        binding = _binding(expected_binding)
        current = _timestamp(now)
        connection = self._connect(create=False)
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._recheck_attachment()
            review = self._review(
                self._row(connection, review_id), owner_id=owner_id, now=current
            )
            if review.binding_digest != binding:
                raise NativeOwnerReviewDenied
            if review.state != "prepared":
                raise NativeOwnerReviewConflict
            if review.expired:
                raise NativeOwnerReviewDenied
            updated = connection.execute(
                "UPDATE owner_reviews SET state = 'accepted' "
                "WHERE review_id = ? AND state = 'prepared'",
                (review.review_id,),
            )
            if updated.rowcount != 1:
                raise NativeOwnerReviewConflict
            self._validate_sidecars(self.database_path)
            connection.commit()
            return self._review(
                self._row(connection, review.review_id), owner_id=owner_id, now=current
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def begin(self, review_id: str, *, owner_id: str, now: int) -> OwnerReview:
        return self._transition(
            review_id,
            owner_id=owner_id,
            now=now,
            before="accepted",
            after="applying",
            require_live=True,
        )

    def reset_before_execution(
        self, review_id: str, *, owner_id: str, now: int
    ) -> OwnerReview:
        return self._transition(
            review_id,
            owner_id=owner_id,
            now=now,
            before="applying",
            after="accepted",
            require_live=False,
        )

    def complete(
        self,
        review_id: str,
        *,
        owner_id: str,
        result: dict[str, Any],
        now: int,
    ) -> OwnerReview:
        result_json = _encode_object(result)
        current = _timestamp(now)
        connection = self._connect(create=False)
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._recheck_attachment()
            review = self._review(
                self._row(connection, review_id), owner_id=owner_id, now=current
            )
            if review.state != "applying":
                raise NativeOwnerReviewConflict
            existing_size = len(_encode_object(_thaw(review.body)).encode("utf-8")) + len(
                _encode_object(_thaw(review.display)).encode("utf-8")
            )
            if existing_size + len(result_json.encode("utf-8")) > MAX_REVIEW_JSON_BYTES:
                raise ValueError("OWNER_REVIEW_TOO_LARGE")
            updated = connection.execute(
                "UPDATE owner_reviews SET state = 'completed', result_json = ? "
                "WHERE review_id = ? AND state = 'applying'",
                (result_json, review.review_id),
            )
            if updated.rowcount != 1:
                raise NativeOwnerReviewConflict
            if (
                review.action == "activation"
                and review.body.get("renewal") == "same-authority"
            ):
                self._publish_renewal(connection, review.review_id)
            self._validate_sidecars(self.database_path)
            connection.commit()
            return self._review(
                self._row(connection, review.review_id), owner_id=owner_id, now=current
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def enable_renewal(
        self, review_id: str, *, owner_id: str, now: int
    ) -> OwnerReview:
        current = _timestamp(now)
        connection = self._connect(create=False)
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._recheck_attachment()
            review = self._review(
                self._row(connection, review_id), owner_id=owner_id, now=current
            )
            if (
                review.state != "completed"
                or review.action != "activation"
                or review.body.get("renewal") != "same-authority"
            ):
                raise NativeOwnerReviewDenied
            self._publish_renewal(connection, review.review_id)
            connection.commit()
            return review
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _publish_renewal(connection: sqlite3.Connection, review_id: str) -> None:
        connection.execute(
            "INSERT INTO settings(name, value) VALUES ('renewal_review_id', ?) "
            "ON CONFLICT(name) DO UPDATE SET value=excluded.value",
            (review_id,),
        )

    def renewal_review(self, *, owner_id: str, now: int) -> OwnerReview | None:
        current = _timestamp(now)
        connection = self._connect(create=False)
        try:
            row = connection.execute(
                "SELECT value FROM settings WHERE name='renewal_review_id'"
            ).fetchone()
            if row is None:
                return None
            try:
                review = self._review(
                    self._row(connection, row[0]), owner_id=owner_id, now=current
                )
            except NativeOwnerReviewDenied:
                raise NativeOwnerReviewUnavailable from None
            if (
                review.state != "completed"
                or review.action != "activation"
                or review.body.get("renewal") != "same-authority"
            ):
                raise NativeOwnerReviewUnavailable
            return review
        finally:
            connection.close()
