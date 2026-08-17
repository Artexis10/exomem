"""Durable, content-free handoff state for full epistemic-graph rebuilds."""

from __future__ import annotations

import atexit
import contextlib
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
import unicodedata
from collections.abc import Callable, Iterable
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO

if TYPE_CHECKING:
    from .mutation_lock import _SecureDirectory
    from .vault import PlannedWrite


_DOMAIN = b"exomem-graph-sync-checkpoint:v1\0"
_FLOOR_DOMAIN = b"exomem-graph-sync-generation-floor:v1\0"
_VERSION = 1
_PATH_LIMIT = 1_000
_TEMP_PREFIX = ".graph-rebuild-"
_CHECKPOINT_FILENAME = ".graph-sync.json"
_FLOOR_FILENAME = ".graph-sync-floor.json"
_RECEIPT_DIRNAME = ".graph-commit-receipts"
_RESET_PREFIX = ".graph-reset-"
_RESET_MANIFEST = ".manifest.json"
_RESET_MEMBERS = (
    ".graph.sqlite",
    ".graph.sqlite-journal",
    ".graph.sqlite-wal",
    ".graph.sqlite-shm",
    _CHECKPOINT_FILENAME,
    _FLOOR_FILENAME,
)
# A maximal v1 paths checkpoint is 1,000 paths of 1,024 UTF-8 bytes plus
# hashes and a duplicated created-path array. Keep a small format margin while
# still refusing arbitrary multi-megabyte synced inputs.
_CHECKPOINT_READ_LIMIT = 3_200_000
_RECEIPT_READ_LIMIT = 65_536
_FLOOR_READ_LIMIT = 8_192
# Re-reads of a *busy* epoch, not of a broken one. The sidecar reader already
# waits out SQLite's own busy timeout, so reaching here means a publication is
# genuinely mid-flight; a couple of short looks is enough to cross it, and
# anything longer belongs to the caller's own retry rather than to this read.
_EPOCH_BUSY_ATTEMPTS = 3
_EPOCH_BUSY_BACKOFF_SECONDS = 0.05
_MUTATION_ID = re.compile(r"^[0-9a-f]{24}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_RECEIPT_TAG = re.compile(r"^[0-9a-f]{16}$")
_WINDOWS_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")
_WINDOWS_RESERVED_NAMES = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        "clock$",
        "conin$",
        "conout$",
        *(f"com{index}" for index in range(1, 10)),
        *(f"lpt{index}" for index in range(1, 10)),
        *(f"com{index}" for index in "¹²³"),
        *(f"lpt{index}" for index in "¹²³"),
    }
)
_RECEIPT_KEY_DOMAIN = b"exomem-graph-commit-receipt-key:v1\0"
_RECEIPT_TERMINAL_DOMAIN = b"exomem-graph-commit-receipt-terminal:v1\0"
_RECEIPT_AUTH_DOMAIN = b"exomem-graph-commit-receipt-auth:v2\0"
_RECEIPT_TERMINAL_FIELDS = frozenset(
    {
        "_terminal",
        "version",
        "ok",
        "state",
        "status",
        "committed",
        "mutated",
        "terminal",
        "warnings_count",
        "request_id",
        "receipt_id",
        "operation_id",
        "result_sha256",
    }
)
_RECEIPT_TERMINAL_STATES = frozenset({"committed", "rejected"})
_RECEIPT_TERMINAL_STATUSES = frozenset({"committed", "replayed", "rejected"})
MAX_GRAPH_REBUILD_WAITERS = 128
MAX_GRAPH_REBUILD_ATTEMPTS = 4

#: An interactive mutation polls a registered graph rebuild; it never waits.
#:
#: There is deliberately no tunable interval here. #588 tried one and it was
#: over-constrained by construction: any bound has to be under `COMMIT_MEDIAN_MS
#: = 750` or the median latency gate fails the moment the bound is reached, and
#: simultaneously long enough for a small test-vault rebuild to converge or a
#: dozen tests that expect `completed` get `pending`. A 2.0 s bound failed the
#: first constraint (2091 ms / 2259 ms observed at 2k/8k pages); a 0.25 s bound
#: failed the second (2 CI failures became 6). Those two constraints do not
#: overlap reliably, which is the design saying the join does not belong on the
#: write path at all.
#:
#: Polling is the honest resolution. The registered-rebuild join is only ever
#: reached *after* the incremental refresh has already fallen back to a
#: full-corpus rebuild, and a full-corpus pass costs 20-175 s -- so at the
#: moment this seam is reached, waiting is never going to be cheap. Report the
#: flight's outcome if it has already settled, otherwise report `pending` and
#: let it converge behind the response. The contribution to commit latency is
#: then provably zero rather than bounded by a number someone has to defend.
_SETTLED_JOIN_TIMEOUT_SECONDS = 0.0

#: `reconcile` is an internal registry name, not something a caller can run.
#: The MCP surface is `maintain_memory(mode="reconcile")` and the CLI dispatches
#: on the public product names (`exomem maintain --reconcile`); "run reconcile"
#: matched neither, so a user following the remediation got a usage error from
#: the server argument parser — the same defect as the doctor strings in #479.
_RECONCILE_CALL = (
    'maintain_memory(mode="reconcile") — `exomem maintain --reconcile` from a shell'
)
_RECONCILE_HINT = f"run {_RECONCILE_CALL} to recover the derived graph."
_RECONCILE_REMEDIATION = f"Run {_RECONCILE_CALL} to recover the derived graph."
_RETRY_OR_RECONCILE = f"Retry the same mutation identity, or {_RECONCILE_HINT}"
_COORDINATORS: dict[str, GraphRebuildCoordinator] = {}
_COORDINATORS_LOCK = threading.Lock()
_LIVE_TEMPORARIES: set[Path] = set()
_REBUILD_LOCK_HANDLES: dict[str, _HeldRebuildLock] = {}
_PENDING_WAITERS: ContextVar[
    dict[str, tuple[GraphRebuildRegistration | GraphRebuildWaiter, GraphSyncCheckpoint]] | None
] = (
    ContextVar("exomem_pending_graph_rebuild_waiters", default=None)
)
logger = logging.getLogger(__name__)


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _is_exact_canonical_json(raw: str | bytes, value: object) -> bool:
    """Require v2 receipt bytes to be the protocol's one canonical UTF-8 form."""
    try:
        rendered = raw.encode("utf-8") if isinstance(raw, str) else raw
    except UnicodeEncodeError:
        return False
    return isinstance(rendered, bytes) and rendered == _canonical_json(value)


def limit_graph_metadata_read(conn: sqlite3.Connection) -> None:
    """Bound untrusted sidecar TEXT before SQLite allocates it for parsing."""
    setlimit = getattr(conn, "setlimit", None)
    category = getattr(sqlite3, "SQLITE_LIMIT_LENGTH", None)
    if callable(setlimit) and isinstance(category, int):
        setlimit(category, _CHECKPOINT_READ_LIMIT)


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON field")
        value[key] = item
    return value


def _is_canonical_relative_path(value: object) -> bool:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        return False
    if unicodedata.normalize("NFC", value) != value:
        return False
    if value.startswith("/") or value.endswith("/") or _WINDOWS_DRIVE_PREFIX.match(value):
        return False
    for part in value.split("/"):
        if (
            part in {"", ".", ".."}
            or part.endswith((".", " "))
            or any(character in '<>:"|?*\x00' or ord(character) < 32 for character in part)
        ):
            return False
        # Windows strips spaces/dots from the basename before resolving a
        # device name; make the portable parser reject that alias too.
        stem = part.split(".", 1)[0].rstrip(" .").casefold()
        if stem in _WINDOWS_RESERVED_NAMES:
            return False
    return True


def _is_digest(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _is_receipt_terminal_projection(value: object) -> bool:
    """Validate the closed, scalar receipt projection before HMAC verification."""
    if not isinstance(value, dict) or not value:
        return False
    for key, item in value.items():
        if key not in _RECEIPT_TERMINAL_FIELDS:
            return False
        if key == "_terminal" and item != "exomem.mutation-terminal":
            return False
        if key == "version" and (type(item) is not int or item != 1):
            return False
        if key in {"ok", "committed", "mutated", "terminal"} and type(item) is not bool:
            return False
        if key == "state" and item not in _RECEIPT_TERMINAL_STATES:
            return False
        if key == "status" and item not in _RECEIPT_TERMINAL_STATUSES:
            return False
        if key in {"request_id", "operation_id"} and (
            not isinstance(item, str) or _UUID.fullmatch(item) is None
        ):
            return False
        if key == "receipt_id" and item is not None and (
            not isinstance(item, str) or _RECEIPT_TAG.fullmatch(item) is None
        ):
            return False
        if key == "warnings_count" and (type(item) is not int or item < 0):
            return False
        if key == "result_sha256" and not _is_digest(item):
            return False
    return True


def namespaced_idempotency_key_digest(namespace: str, key: str) -> str:
    """Hash an idempotency key in its replay namespace without retaining either."""
    if not isinstance(namespace, str) or not namespace or not isinstance(key, str) or not key:
        raise ValueError("receipt key namespace and key must be non-empty strings")
    return hashlib.sha256(
        _RECEIPT_KEY_DOMAIN + namespace.encode("utf-8") + b"\0" + key.encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True)
class GraphSyncCheckpoint:
    """The closed v1 canonical-to-derived graph handoff record."""

    version: int
    generation: int
    mutation_id: str
    scope: str
    paths: tuple[tuple[str, str | None], ...]
    created_paths: tuple[str, ...]
    checkpoint_sha256: str

    @classmethod
    def create(
        cls,
        *,
        generation: int,
        mutation_id: str,
        paths: tuple[tuple[str, str | None], ...],
        created_paths: tuple[str, ...],
        scope: str = "paths",
    ) -> GraphSyncCheckpoint:
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
            raise ValueError("generation must be a positive integer")
        if not isinstance(mutation_id, str) or _MUTATION_ID.fullmatch(mutation_id) is None:
            raise ValueError("mutation_id must be lowercase 24-hex")
        if scope not in {"paths", "full"}:
            raise ValueError("scope must be paths or full")
        raw_paths = tuple(paths)
        raw_created = tuple(created_paths)
        if scope == "full":
            # Existing callers pass the changed paths while escalating to a
            # full checkpoint.  V1's canonical full form intentionally drops
            # both arrays, so retain that compatible rendering behaviour.
            ordered_paths: tuple[tuple[str, str | None], ...] = ()
            ordered_created: tuple[str, ...] = ()
        else:
            if len(raw_paths) > _PATH_LIMIT:
                raise ValueError("paths cannot exceed 1000 entries")
            if len(raw_created) > _PATH_LIMIT:
                raise ValueError("created paths cannot exceed 1000 entries")
            if any(
                not _is_canonical_relative_path(path)
                or (content_hash is not None and not _is_digest(content_hash))
                for path, content_hash in raw_paths
            ):
                raise ValueError("paths must be canonical and carry lowercase SHA-256 hashes")
            if len({path for path, _content_hash in raw_paths}) != len(raw_paths):
                raise ValueError("paths must be unique")
            if any(not _is_canonical_relative_path(path) for path in raw_created):
                raise ValueError("created paths must be canonical")
            if len(set(raw_created)) != len(raw_created):
                raise ValueError("created paths must be unique")
            non_null_paths = {path for path, content_hash in raw_paths if content_hash is not None}
            if not set(raw_created).issubset(non_null_paths):
                raise ValueError("created paths must be a subset of non-null paths")
            ordered_paths = tuple(sorted(raw_paths))
            ordered_created = tuple(sorted(raw_created))
        value = {
            "version": _VERSION,
            "generation": generation,
            "mutation_id": mutation_id,
            "scope": scope,
            "paths": [list(item) for item in ordered_paths],
            "created_paths": list(ordered_created),
        }
        digest = hashlib.sha256(_DOMAIN + _canonical_json(value)).hexdigest()
        return cls(
            _VERSION,
            generation,
            mutation_id,
            scope,
            ordered_paths,
            ordered_created,
            digest,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "generation": self.generation,
            "mutation_id": self.mutation_id,
            "scope": self.scope,
            "paths": [list(item) for item in self.paths],
            "created_paths": list(self.created_paths),
            "checkpoint_sha256": self.checkpoint_sha256,
        }

    def render(self) -> str:
        return _canonical_json(self.as_dict()).decode("utf-8")

    @classmethod
    def parse(cls, raw: str | bytes) -> GraphSyncCheckpoint | None:
        try:
            value = json.loads(raw, object_pairs_hook=_reject_duplicate_json_keys)
            if not isinstance(value, dict) or set(value) != {
                "version",
                "generation",
                "mutation_id",
                "scope",
                "paths",
                "created_paths",
                "checkpoint_sha256",
            }:
                return None
            version = value["version"]
            generation = value["generation"]
            mutation_id = value["mutation_id"]
            scope = value["scope"]
            paths = value["paths"]
            created_paths = value["created_paths"]
            digest = value["checkpoint_sha256"]
            if (
                type(version) is not int
                or version != _VERSION
                or type(generation) is not int
                or generation < 1
                or not isinstance(mutation_id, str)
                or _MUTATION_ID.fullmatch(mutation_id) is None
                or not isinstance(scope, str)
                or scope not in {"paths", "full"}
                or not isinstance(paths, list)
                or not isinstance(created_paths, list)
                or not _is_digest(digest)
            ):
                return None
            if len(paths) > _PATH_LIMIT or len(created_paths) > _PATH_LIMIT:
                return None
            normalized_paths: list[tuple[str, str | None]] = []
            for item in paths:
                if (
                    not isinstance(item, list)
                    or len(item) != 2
                    or not isinstance(item[0], str)
                    or (item[1] is not None and not _is_digest(item[1]))
                    or not _is_canonical_relative_path(item[0])
                ):
                    return None
                normalized_paths.append((item[0], item[1]))
            if not all(_is_canonical_relative_path(item) for item in created_paths):
                return None
            if scope == "full" and (normalized_paths or created_paths):
                return None
            if scope == "paths" and (
                len({path for path, _content_hash in normalized_paths}) != len(normalized_paths)
                or len(set(created_paths)) != len(created_paths)
                or not set(created_paths).issubset(
                    {path for path, content_hash in normalized_paths if content_hash is not None}
                )
            ):
                return None
            # Checkpoints are canonical bytes, not a loose semantic form.  Do
            # not accept inputs that `create()` would normalize before hashing.
            if scope == "paths" and (
                tuple(normalized_paths) != tuple(sorted(normalized_paths))
                or tuple(created_paths) != tuple(sorted(created_paths))
            ):
                return None
            checkpoint = cls.create(
                generation=generation,
                mutation_id=mutation_id,
                paths=tuple(normalized_paths),
                created_paths=tuple(created_paths),
                scope=scope,
            )
            return checkpoint if checkpoint.checkpoint_sha256 == digest else None
        except (RecursionError, TypeError, ValueError, UnicodeDecodeError):
            return None


@dataclass(frozen=True)
class GraphCommitReceipt:
    """Closed, content-free evidence for one claimed canonical graph write.

    This receipt is intentionally a second protocol step: its terminal
    projection exists only after the caller's canonical batch has returned.
    Therefore a crash after canonical files but before a committed receipt is
    still outcome-unknown and must never be inferred from vault similarity.
    """

    version: int
    idempotency_key_digest: str
    command_digest: str
    attempt_id: str
    commit_token: str
    canonical_disposition: str | None
    terminal_projection: dict[str, Any]
    terminal_projection_sha256: str
    checkpoint_generation: int | None
    checkpoint_sha256: str | None
    receipt_hmac_sha256: str | None
    # V1 only. It remains parseable as portable advisory evidence, but no
    # current code may treat it as authority for a canonical retry.
    commit_point: bool | None = None

    @classmethod
    def create(
        cls,
        *,
        idempotency_key_digest: str,
        command_digest: str,
        attempt_id: str,
        commit_token: str,
        canonical_disposition: str,
        terminal_projection: dict[str, Any],
        checkpoint_generation: int | None = None,
        checkpoint_sha256: str | None = None,
        commit_secret: bytes,
    ) -> GraphCommitReceipt:
        if not _is_digest(idempotency_key_digest) or not _is_digest(command_digest):
            raise ValueError("receipt digests must be lowercase SHA-256")
        if _MUTATION_ID.fullmatch(attempt_id) is None or _MUTATION_ID.fullmatch(commit_token) is None:
            raise ValueError("receipt attempt and commit token must be lowercase 24-hex")
        if canonical_disposition not in {"success", "committed_failure"}:
            raise ValueError("receipt canonical disposition is invalid")
        if (checkpoint_generation is None) != (checkpoint_sha256 is None):
            raise ValueError("receipt checkpoint generation and digest must be paired")
        if checkpoint_generation is not None and (
            type(checkpoint_generation) is not int
            or checkpoint_generation < 1
            or not _is_digest(checkpoint_sha256)
        ):
            raise ValueError("receipt checkpoint is invalid")
        if not isinstance(commit_secret, bytes) or len(commit_secret) != 32:
            raise ValueError("receipt commit secret must be exactly 32 bytes")
        if not _is_receipt_terminal_projection(terminal_projection):
            raise ValueError("receipt terminal projection must be bounded and content-free")
        projected = dict(sorted(terminal_projection.items()))
        terminal_digest = hashlib.sha256(
            _RECEIPT_TERMINAL_DOMAIN + _canonical_json(projected)
        ).hexdigest()
        unsigned = {
            "version": 2,
            "idempotency_key_digest": idempotency_key_digest,
            "command_digest": command_digest,
            "attempt_id": attempt_id,
            "commit_token": commit_token,
            "canonical_disposition": canonical_disposition,
            "terminal_projection": projected,
            "terminal_projection_sha256": terminal_digest,
            "checkpoint_generation": checkpoint_generation,
            "checkpoint_sha256": checkpoint_sha256,
        }
        return cls(
            2,
            idempotency_key_digest,
            command_digest,
            attempt_id,
            commit_token,
            canonical_disposition,
            projected,
            terminal_digest,
            checkpoint_generation,
            checkpoint_sha256,
            hmac.new(
                commit_secret, _RECEIPT_AUTH_DOMAIN + _canonical_json(unsigned), hashlib.sha256
            ).hexdigest(),
        )

    def as_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "version": self.version,
            "idempotency_key_digest": self.idempotency_key_digest,
            "command_digest": self.command_digest,
            "attempt_id": self.attempt_id,
            "commit_token": self.commit_token,
        }
        if self.version == 2:
            value["canonical_disposition"] = self.canonical_disposition
            value["terminal_projection"] = self.terminal_projection
            value["terminal_projection_sha256"] = self.terminal_projection_sha256
            value["checkpoint_generation"] = self.checkpoint_generation
            value["checkpoint_sha256"] = self.checkpoint_sha256
            value["receipt_hmac_sha256"] = self.receipt_hmac_sha256
        else:
            value["terminal_projection"] = self.terminal_projection
            value["terminal_projection_sha256"] = self.terminal_projection_sha256
            value["checkpoint_generation"] = self.checkpoint_generation
            value["checkpoint_sha256"] = self.checkpoint_sha256
            value["commit_point"] = self.commit_point
        return value

    def render(self) -> str:
        return _canonical_json(self.as_dict()).decode("utf-8")

    def verify(
        self,
        commit_secret: bytes,
        *,
        idempotency_key_digest: str,
        command_digest: str,
        attempt_id: str,
        commit_token: str,
    ) -> bool:
        """Verify v2 receipt authority against the private local attempt key."""
        if (
            self.version != 2
            or not isinstance(commit_secret, bytes)
            or len(commit_secret) != 32
            or not isinstance(self.receipt_hmac_sha256, str)
            or self.idempotency_key_digest != idempotency_key_digest
            or self.command_digest != command_digest
            or self.attempt_id != attempt_id
            or self.commit_token != commit_token
            or self.canonical_disposition not in {"success", "committed_failure"}
        ):
            return False
        unsigned = self.as_dict()
        unsigned.pop("receipt_hmac_sha256", None)
        expected = hmac.new(
            commit_secret, _RECEIPT_AUTH_DOMAIN + _canonical_json(unsigned), hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(self.receipt_hmac_sha256, expected)

    @classmethod
    def parse(cls, raw: str | bytes) -> GraphCommitReceipt | None:
        try:
            value = json.loads(raw, object_pairs_hook=_reject_duplicate_json_keys)
            if not isinstance(value, dict):
                return None
            legacy_common = {
                "version",
                "idempotency_key_digest",
                "command_digest",
                "attempt_id",
                "commit_token",
                "terminal_projection",
                "terminal_projection_sha256",
                "checkpoint_generation",
                "checkpoint_sha256",
            }
            version = value.get("version")
            if version == 2:
                if (
                    set(value) != legacy_common | {"canonical_disposition", "receipt_hmac_sha256"}
                    or not _is_digest(value["receipt_hmac_sha256"])
                    or not _is_exact_canonical_json(raw, value)
                ):
                    return None
                receipt = cls.create(
                    idempotency_key_digest=value["idempotency_key_digest"],
                    command_digest=value["command_digest"],
                    attempt_id=value["attempt_id"],
                    commit_token=value["commit_token"],
                    canonical_disposition=value["canonical_disposition"],
                    terminal_projection=value["terminal_projection"],
                    checkpoint_generation=value["checkpoint_generation"],
                    checkpoint_sha256=value["checkpoint_sha256"],
                    # Parsing validates structure first; this throwaway key
                    # is never authority. `verify()` checks the stored MAC.
                    commit_secret=b"\0" * 32,
                )
                if (
                    not _is_digest(value["terminal_projection_sha256"])
                    or value["terminal_projection_sha256"]
                    != receipt.terminal_projection_sha256
                ):
                    return None
                return cls(
                    receipt.version,
                    receipt.idempotency_key_digest,
                    receipt.command_digest,
                    receipt.attempt_id,
                    receipt.commit_token,
                    receipt.canonical_disposition,
                    receipt.terminal_projection,
                    receipt.terminal_projection_sha256,
                    receipt.checkpoint_generation,
                    receipt.checkpoint_sha256,
                    value["receipt_hmac_sha256"],
                )
            if set(value) != legacy_common | {"commit_point"}:
                return None
            if type(value["version"]) is not int or value["version"] != 1 or type(value["commit_point"]) is not bool:
                return None
            # V1 is deliberately parse-only: it has no private proof and
            # must never promote an executing local mutation.
            projected = value["terminal_projection"]
            if not isinstance(projected, dict) or not projected:
                return None
            receipt = cls.create(
                idempotency_key_digest=value["idempotency_key_digest"],
                command_digest=value["command_digest"],
                attempt_id=value["attempt_id"],
                commit_token=value["commit_token"],
                canonical_disposition="success",
                terminal_projection=projected,
                checkpoint_generation=value["checkpoint_generation"],
                checkpoint_sha256=value["checkpoint_sha256"],
                commit_secret=b"\0" * 32,
            )
            legacy = cls(
                1,
                receipt.idempotency_key_digest,
                receipt.command_digest,
                receipt.attempt_id,
                receipt.commit_token,
                None,
                receipt.terminal_projection,
                receipt.terminal_projection_sha256,
                receipt.checkpoint_generation,
                receipt.checkpoint_sha256,
                None,
                value["commit_point"],
            )
            return (
                legacy
                if value["terminal_projection_sha256"] == legacy.terminal_projection_sha256
                and legacy.as_dict() == value
                else None
            )
        except (RecursionError, TypeError, ValueError, UnicodeDecodeError):
            return None


@dataclass(frozen=True)
class GraphSyncGenerationFloor:
    """Closed internal proof that a graph-sync generation was issued."""

    version: int
    generation: int
    floor_sha256: str

    @classmethod
    def create(cls, generation: int) -> GraphSyncGenerationFloor:
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
            raise ValueError("generation must be a positive integer")
        value = {"version": _VERSION, "generation": generation}
        return cls(
            _VERSION,
            generation,
            hashlib.sha256(_FLOOR_DOMAIN + _canonical_json(value)).hexdigest(),
        )

    def as_dict(self) -> dict[str, int | str]:
        return {
            "version": self.version,
            "generation": self.generation,
            "floor_sha256": self.floor_sha256,
        }

    def render(self) -> str:
        return _canonical_json(self.as_dict()).decode("utf-8")

    @classmethod
    def parse(cls, raw: str | bytes) -> GraphSyncGenerationFloor | None:
        try:
            value = json.loads(raw, object_pairs_hook=_reject_duplicate_json_keys)
            if not isinstance(value, dict) or set(value) != {
                "version",
                "generation",
                "floor_sha256",
            }:
                return None
            if (
                type(value["version"]) is not int
                or value["version"] != _VERSION
                or type(value["generation"]) is not int
            ):
                return None
            if not isinstance(value["floor_sha256"], str):
                return None
            floor = cls.create(value["generation"])
            return floor if floor.floor_sha256 == value["floor_sha256"] else None
        except (RecursionError, TypeError, ValueError, UnicodeDecodeError):
            return None


@dataclass(frozen=True)
class GraphDeletionEpoch:
    """Recoverable floor-before-rename graph epoch for one lifecycle transition."""

    vault_root: Path
    checkpoint: GraphSyncCheckpoint
    prior_floor_bytes: bytes | None
    prior_checkpoint_bytes: bytes | None


class GraphLifecycleRollbackError(RuntimeError):
    """A caught lifecycle transition could not be restored safely."""


class GraphLifecycleEpochSetupError(RuntimeError):
    """Epoch staging failed and was rolled back before a lifecycle rename."""


@dataclass
class GraphLifecycleTransition:
    """One reversible lifecycle rename and its graph epoch handoff.

    The lifecycle marker is deliberately created before the floor.  The floor
    then makes an abrupt post-rename interruption durable for reconcile.  A
    caught failure takes the inverse route here, rather than allowing each
    caller to independently forget one half of that protocol.
    """

    operation: Any
    source: Path
    destination: Path
    recovery: bool
    epoch: GraphDeletionEpoch | None
    renamed: bool = False
    completed: bool = False

    def rename(self) -> None:
        from .governance import lifecycle

        try:
            lifecycle.atomic_rename(
                self.operation,
                source=self.source,
                destination=self.destination,
                recovery=self.recovery,
            )
        except lifecycle._PostRenameDurabilityError as error:
            self.renamed = True
            raise lifecycle.LifecycleError(error.code, error.reason) from error
        self.renamed = True

    def publish_checkpoint(self) -> None:
        if self.epoch is not None:
            commit_deletion_epoch(self.epoch)
        self.completed = True

    def abort(self) -> None:
        """Reverse a caught pre-commit transition or retain honest evidence."""
        from .governance import lifecycle

        try:
            if self.renamed:
                lifecycle.atomic_rename(
                    self.operation,
                    source=self.destination,
                    destination=self.source,
                    recovery=not self.recovery,
                )
                self.renamed = False
            if self.epoch is not None:
                restore_deletion_epoch(self.epoch)
            if self.recovery:
                lifecycle.abort_recovery(self.operation)
            else:
                lifecycle.abort_deletion(self.operation)
        except Exception as error:  # noqa: BLE001 - retain lifecycle evidence for reconcile
            raise GraphLifecycleRollbackError(
                "lifecycle transition could not be restored; reconcile is required"
            ) from error


def begin_deletion_transition(
    vault_root: Path,
    *,
    source_rel: str,
    trash_rel: str,
    removed_rel_paths: Iterable[str],
) -> GraphLifecycleTransition:
    """Create the deletion marker and staged graph floor as one abortable unit."""
    from .governance import lifecycle

    operation = lifecycle.begin_deletion(
        vault_root, source_rel=source_rel, trash_rel=trash_rel
    )
    root = Path(vault_root)
    prior_floor: bytes | None = None
    prior_checkpoint: bytes | None = None
    captured_prior = False
    epoch_staging_mutated = False
    try:
        prior_floor = _prior_artifact_bytes(floor_path(root))
        prior_checkpoint = _prior_artifact_bytes(checkpoint_path(root))
        captured_prior = True
        epoch = prepare_deletion_epoch(vault_root, removed_rel_paths)
    except Exception as error:
        try:
            if captured_prior:
                epoch_staging_mutated = _epoch_artifacts_changed(
                    root, prior_floor, prior_checkpoint
                )
            if epoch_staging_mutated:
                _restore_epoch_artifacts(root, prior_floor, prior_checkpoint)
            lifecycle.abort_deletion(operation)
        except Exception as rollback_error:  # noqa: BLE001 - preserve durable repair evidence
            raise GraphLifecycleRollbackError(
                "staged deletion epoch could not be restored; reconcile is required"
            ) from rollback_error
        raise GraphLifecycleEpochSetupError(
            "could not establish the graph deletion epoch"
        ) from error
    return GraphLifecycleTransition(
        operation,
        Path(vault_root) / source_rel,
        Path(vault_root) / trash_rel,
        False,
        epoch,
    )


def begin_recovery_transition(
    vault_root: Path,
    *,
    trash_rel: str,
    source_rel: str,
    restored_paths: Iterable[tuple[str, str]],
) -> GraphLifecycleTransition:
    """Create recovery evidence and its staged graph floor under one abort path."""
    from .governance import lifecycle

    operation = lifecycle.begin_recovery(
        vault_root, trash_rel=trash_rel, source_rel=source_rel
    )
    root = Path(vault_root)
    prior_floor: bytes | None = None
    prior_checkpoint: bytes | None = None
    captured_prior = False
    epoch_staging_mutated = False
    try:
        prior_floor = _prior_artifact_bytes(floor_path(root))
        prior_checkpoint = _prior_artifact_bytes(checkpoint_path(root))
        captured_prior = True
        epoch = prepare_recovery_epoch(vault_root, restored_paths)
    except Exception as error:
        try:
            if captured_prior:
                epoch_staging_mutated = _epoch_artifacts_changed(
                    root, prior_floor, prior_checkpoint
                )
            if epoch_staging_mutated:
                _restore_epoch_artifacts(root, prior_floor, prior_checkpoint)
            lifecycle.abort_recovery(operation)
        except Exception as rollback_error:  # noqa: BLE001 - preserve durable repair evidence
            raise GraphLifecycleRollbackError(
                "staged recovery epoch could not be restored; reconcile is required"
            ) from rollback_error
        raise GraphLifecycleEpochSetupError(
            "could not establish the graph recovery epoch"
        ) from error
    return GraphLifecycleTransition(
        operation,
        Path(vault_root) / trash_rel,
        Path(vault_root) / source_rel,
        True,
        epoch,
    )


def next_checkpoint(
    *,
    current: GraphSyncCheckpoint | None,
    acknowledged_generation: int,
    floor_generation: int = 0,
    mutation_id: str,
    paths: list[tuple[str, str | None]],
    created_paths: list[str],
    force_full_scope: bool = False,
) -> GraphSyncCheckpoint:
    generation = max(current.generation if current else 0, acknowledged_generation, floor_generation) + 1
    scope = "full" if force_full_scope or len(paths) > _PATH_LIMIT else "paths"
    return GraphSyncCheckpoint.create(
        generation=generation,
        mutation_id=mutation_id,
        paths=tuple(paths),
        created_paths=tuple(created_paths),
        scope=scope,
    )


def checkpoint_path(vault_root: Path) -> Path:
    from .kbdir import kb_dirname

    return Path(vault_root) / kb_dirname() / _CHECKPOINT_FILENAME


def floor_path(vault_root: Path) -> Path:
    from .kbdir import kb_dirname

    return Path(vault_root) / kb_dirname() / _FLOOR_FILENAME


def graph_commit_receipt_path(vault_root: Path, commit_token: str) -> Path:
    """Return the hidden portable receipt location for one opaque claim token."""
    if _MUTATION_ID.fullmatch(commit_token) is None:
        raise ValueError("receipt commit token must be lowercase 24-hex")
    from .kbdir import kb_dirname

    return Path(vault_root) / kb_dirname() / _RECEIPT_DIRNAME / f"{commit_token}.json"


def read_graph_commit_receipt(
    vault_root: Path, commit_token: str
) -> GraphCommitReceipt | None:
    try:
        raw = _read_bounded_bytes(
            graph_commit_receipt_path(vault_root, commit_token),
            limit=_RECEIPT_READ_LIMIT,
            vault_root=Path(vault_root),
        )
    except (FileNotFoundError, OSError, ValueError):
        return None
    return GraphCommitReceipt.parse(raw)


def write_graph_commit_receipt(vault_root: Path, receipt: GraphCommitReceipt) -> Path:
    """Persist one receipt after a caller batch without manufacturing a commit.

    The caller leaf's terminal projection does not exist until its canonical
    batch has returned.  This intentionally leaves the canonical-file-before-
    receipt cut outcome-unknown until the later idempotency protocol writes the
    receipt marker and advances its retry row.
    """
    from .vault import PlannedWrite, batch_atomic_write

    path = graph_commit_receipt_path(vault_root, receipt.commit_token)
    batch_atomic_write(
        [PlannedWrite(path, receipt.render())],
        vault_root=vault_root,
        post_commit_fanout=False,
        commit_point=False,
    )
    return path


def floor_state(vault_root: Path) -> tuple[str, GraphSyncGenerationFloor | None]:
    try:
        raw = _read_bounded_bytes(
            floor_path(vault_root), limit=_FLOOR_READ_LIMIT, vault_root=Path(vault_root)
        )
    except FileNotFoundError:
        return "absent", None
    except (OSError, ValueError):
        return "malformed", None
    floor = GraphSyncGenerationFloor.parse(raw)
    return ("valid", floor) if floor is not None else ("malformed", None)


def read_floor(vault_root: Path) -> GraphSyncGenerationFloor | None:
    return floor_state(vault_root)[1]


def read_checkpoint(vault_root: Path) -> GraphSyncCheckpoint | None:
    _state, checkpoint = checkpoint_state(vault_root)
    return checkpoint


def checkpoint_state(vault_root: Path) -> tuple[str, GraphSyncCheckpoint | None]:
    """Return whether the durable handoff is absent, valid, or malformed."""
    path = checkpoint_path(vault_root)
    try:
        raw = _read_bounded_bytes(path, limit=_CHECKPOINT_READ_LIMIT, vault_root=Path(vault_root))
    except FileNotFoundError:
        return "absent", None
    except (OSError, ValueError):
        return "malformed", None
    checkpoint = GraphSyncCheckpoint.parse(raw)
    return ("valid", checkpoint) if checkpoint is not None else ("malformed", None)


def is_graph_input_path(relative_path: str) -> bool:
    normalized = relative_path.replace("\\", "/")
    return (
        not normalized.endswith((f"/{_CHECKPOINT_FILENAME}", f"/{_FLOOR_FILENAME}"))
        and f"/{_RECEIPT_DIRNAME}/" not in f"/{normalized.lstrip('/')}"
    )


def acknowledged_generation(vault_root: Path) -> int:
    acknowledged = acknowledged_checkpoint(vault_root)
    return acknowledged.generation if acknowledged is not None else 0


def acknowledged_checkpoint(vault_root: Path) -> GraphBuildOutcome | None:
    return acknowledgement_state(vault_root)[1]


def _is_sqlite_contention(error: BaseException) -> bool:
    """Is this SQLite refusing *now*, rather than reporting a broken database?

    `sqlite3.OperationalError` carries both, separated only by message text.
    The distinction matters more here than almost anywhere else in the module:
    every caller of `acknowledgement_state` treats a non-readable ack as a
    broken lineage, and a broken lineage fails the write and refuses every
    later repair until someone runs a reconcile. A busy sidecar must not buy
    that verdict -- it is the expected state whenever a rebuild is publishing.
    """
    if not isinstance(error, sqlite3.OperationalError):
        return False
    text = str(error).lower()
    return "locked" in text or "busy" in text


def acknowledgement_state(vault_root: Path) -> tuple[str, GraphBuildOutcome | None]:
    """Return whether the live sidecar has no, valid, malformed, or busy graph ack.

    `busy` is deliberately *not* `malformed`. Contention says nothing about the
    lineage: the reader simply could not look. Folding the two together is what
    made a concurrent write project `GRAPH_SYNC_LINEAGE_CONFLICT` and told the
    caller to reconcile a graph that was perfectly intact.
    """
    from .epistemic_graph import sidecar_path

    path = sidecar_path(vault_root)
    if not path.exists():
        return "absent", None
    try:
        # `closing`, not the connection's own context manager: that one is a
        # transaction scope and leaves the handle open. A leaked read handle
        # here is what makes the next publication's `os.replace` fail on
        # Windows, and this reader is on the graph read path.
        with contextlib.closing(
            sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        ) as conn:
            limit_graph_metadata_read(conn)
            values = dict(
                conn.execute(
                    "SELECT key, value FROM graph_meta WHERE key IN "
                    "('graph_sync_generation', 'graph_sync_digest', 'graph_sync_checkpoint')"
                )
            )
        if not values:
            return "absent", None
        if set(values) != {
            "graph_sync_generation",
            "graph_sync_digest",
            "graph_sync_checkpoint",
        }:
            return "malformed", None
        generation_raw = values["graph_sync_generation"]
        digest = values["graph_sync_digest"]
        rendered_checkpoint = values["graph_sync_checkpoint"]
        if (
            not isinstance(generation_raw, str)
            or not generation_raw.isascii()
            or not generation_raw.isdecimal()
            or generation_raw.startswith("0")
            or not _is_digest(digest)
        ):
            return "malformed", None
        generation = int(generation_raw)
        checkpoint = GraphSyncCheckpoint.parse(rendered_checkpoint)
        if (
            generation < 1
            or checkpoint is None
            or checkpoint.generation != generation
            or checkpoint.checkpoint_sha256 != digest
        ):
            return "malformed", None
        return "valid", GraphBuildOutcome.covering(checkpoint)
    except (KeyError, OSError, sqlite3.Error, ValueError) as error:
        if _is_sqlite_contention(error):
            return "busy", None
        return "malformed", None


def _malformed_acknowledgement_generation(vault_root: Path) -> int | None:
    """Return only a canonical generation hint from an otherwise malformed ack."""
    from .epistemic_graph import sidecar_path

    path = sidecar_path(vault_root)
    if not path.exists():
        return None
    try:
        with contextlib.closing(
            sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        ) as conn:  # `closing` for the reason given in `acknowledgement_state`.
            limit_graph_metadata_read(conn)
            row = conn.execute(
                "SELECT value FROM graph_meta WHERE key = 'graph_sync_generation'"
            ).fetchone()
        value = row[0] if row is not None else None
        if (
            not isinstance(value, str)
            or not value.isascii()
            or not value.isdecimal()
            or value.startswith("0")
        ):
            return None
        generation = int(value)
        return generation if generation >= 1 else None
    except (OSError, sqlite3.Error, ValueError):
        return None


@dataclass(frozen=True)
class GraphPublicationEpoch:
    """A parse-complete epoch that may be used for one private build pass."""

    floor: GraphSyncGenerationFloor | None
    checkpoint: GraphSyncCheckpoint | None
    acknowledgement: GraphBuildOutcome | None


@dataclass(frozen=True)
class GraphEpochState:
    """Classify durable graph epoch inputs before advancing or publishing them."""

    kind: str
    floor: GraphSyncGenerationFloor | None
    checkpoint: GraphSyncCheckpoint | None
    acknowledgement: GraphBuildOutcome | None

    @property
    def requires_full_recovery(self) -> bool:
        return self.kind == "recoverable"


def classify_epoch(vault_root: Path) -> GraphEpochState:
    """Return the only closed floor/checkpoint/ack combinations we can act on.

    A valid floor is an issued-generation proof.  If its matching checkpoint was
    interrupted, the next event must supersede it with full-vault coverage; it
    may not reuse the floor generation or pretend the partial caller batch was
    path-scoped.  Missing or malformed floors remain ambiguous outside the two
    explicitly supported legacy migration states.
    """
    floor_status, floor = floor_state(vault_root)
    checkpoint_status, checkpoint = checkpoint_state(vault_root)
    acknowledgement_status, acknowledgement = acknowledgement_state(vault_root)
    return _classify_epoch_states(
        floor_status,
        floor,
        checkpoint_status,
        checkpoint,
        acknowledgement_status,
        acknowledgement,
    )


def _classify_epoch_states(
    floor_status: str,
    floor: GraphSyncGenerationFloor | None,
    checkpoint_status: str,
    checkpoint: GraphSyncCheckpoint | None,
    acknowledgement_status: str,
    acknowledgement: GraphBuildOutcome | None,
) -> GraphEpochState:
    """Classify already-read states, so a caller can restate one of them.

    Split out for `_admit_epoch_inputs`, which needs to ask what the lineage
    would be if the acknowledgement it could not read were simply not there.
    """
    if acknowledgement_status == "busy":
        # Not a lineage verdict at all: the sidecar was locked, so this read
        # learned nothing about it. Every other kind here is a claim about
        # history; `busy` is a claim about *now*, and the only correct response
        # to it is to look again.
        return GraphEpochState("busy", floor, checkpoint, None)
    if (
        floor_status == "absent"
        and checkpoint_status == "absent"
        and acknowledgement_status == "absent"
    ):
        return GraphEpochState("legacy", None, None, None)
    if (
        floor_status == "absent"
        and checkpoint_status == "valid"
        and checkpoint is not None
        and acknowledgement_status in {"absent", "valid"}
        and (
            acknowledgement is None
            or acknowledgement == GraphBuildOutcome.covering(checkpoint)
        )
    ):
        return GraphEpochState("pre_floor", None, checkpoint, acknowledgement)
    if floor_status != "valid" or floor is None or acknowledgement_status not in {"absent", "valid"}:
        return GraphEpochState("unavailable", floor, checkpoint, acknowledgement)
    if acknowledgement is not None and acknowledgement.generation > floor.generation:
        return GraphEpochState("unavailable", floor, checkpoint, acknowledgement)
    if (
        checkpoint_status == "valid"
        and checkpoint is not None
        and checkpoint.generation == floor.generation
    ):
        if acknowledgement is None or acknowledgement.generation < checkpoint.generation:
            return GraphEpochState("coherent", floor, checkpoint, acknowledgement)
        if acknowledgement == GraphBuildOutcome.covering(checkpoint):
            return GraphEpochState("coherent", floor, checkpoint, acknowledgement)
        return GraphEpochState("unavailable", floor, checkpoint, acknowledgement)
    if checkpoint_status in {"absent", "malformed"} or (
        checkpoint_status == "valid"
        and checkpoint is not None
        and checkpoint.generation < floor.generation
    ):
        return GraphEpochState("recoverable", floor, checkpoint, acknowledgement)
    return GraphEpochState("unavailable", floor, checkpoint, acknowledgement)


def publication_epoch(vault_root: Path) -> GraphPublicationEpoch:
    """Read the only floor/checkpoint/ack states safe to publish over.

    A full builder may consume exact legacy state, accepted pre-floor migration,
    or a valid floor/checkpoint epoch with an absent, predecessor, or exact
    acknowledgement. Any parse failure or contradictory lineage must be
    recovered before it can reach the sidecar replacement seam.
    """
    busy = False
    for attempt in range(_EPOCH_BUSY_ATTEMPTS):
        epoch = classify_epoch(vault_root)
        if epoch.kind in {"legacy", "pre_floor", "coherent"}:
            return GraphPublicationEpoch(
                epoch.floor, epoch.checkpoint, epoch.acknowledgement
            )
        busy = epoch.kind == "busy"
        if busy and attempt + 1 < _EPOCH_BUSY_ATTEMPTS:
            time.sleep(_EPOCH_BUSY_BACKOFF_SECONDS)
    if busy:
        raise GraphEpochUnreadable()
    floor_status, floor = floor_state(vault_root)
    checkpoint_status, checkpoint = checkpoint_state(vault_root)
    acknowledgement_status, _acknowledgement = acknowledgement_state(vault_root)
    malformed_generation = (
        _malformed_acknowledgement_generation(vault_root)
        if acknowledgement_status == "malformed"
        else None
    )
    if (
        floor_status == "valid"
        and floor is not None
        and checkpoint_status == "valid"
        and checkpoint is not None
        and checkpoint.generation == floor.generation
        and checkpoint.scope == "full"
        and malformed_generation is not None
        and malformed_generation < checkpoint.generation
    ):
        return GraphPublicationEpoch(floor, checkpoint, None)
    raise GraphEpochIncoherent("graph floor/checkpoint/ack epoch is not coherent for publication")


def canonical_publication_epoch(vault_root: Path) -> GraphPublicationEpoch:
    """Read the bounded canonical epoch used by a private publication ticket.

    This deliberately consults only the floor and checkpoint artifacts.  The
    sidecar acknowledgement is derived state and must never be opened from the
    short canonical publication hold.
    """
    floor_status, floor = floor_state(vault_root)
    checkpoint_status, checkpoint = checkpoint_state(vault_root)
    if floor_status == "absent" and checkpoint_status == "absent":
        return GraphPublicationEpoch(None, None, None)
    if floor_status == "absent" and checkpoint_status == "valid" and checkpoint is not None:
        return GraphPublicationEpoch(None, checkpoint, None)
    if (
        floor_status == "valid"
        and floor is not None
        and checkpoint_status == "valid"
        and checkpoint is not None
        and checkpoint.generation == floor.generation
    ):
        return GraphPublicationEpoch(floor, checkpoint, None)
    raise GraphEpochIncoherent("graph floor/checkpoint epoch is not coherent for publication")


def _admit_epoch_inputs(
    vault_root: Path,
) -> GraphEpochState:
    for attempt in range(_EPOCH_BUSY_ATTEMPTS):
        epoch = classify_epoch(vault_root)
        if epoch.kind in {"legacy", "pre_floor", "coherent", "recoverable"}:
            return epoch
        if epoch.kind != "busy":
            raise GraphEpochIncoherent("graph floor/checkpoint epoch is malformed or ambiguous")
        if attempt + 1 < _EPOCH_BUSY_ATTEMPTS:
            time.sleep(_EPOCH_BUSY_BACKOFF_SECONDS)
    # This is the canonical write path, and it is the site the busy/malformed
    # conflation hurt most: a locked sidecar failed the mutation outright with
    # `GRAPH_SYNC_LINEAGE_CONFLICT` -- "your graph history is broken" for a
    # sidecar someone else is writing this instant.
    #
    # Admission does not need the acknowledgement. Its only consumer is the
    # `max(...)` in `next_checkpoint`, where the generation floor -- read from
    # its own file, not from the locked sidecar -- already dominates it, since
    # an acknowledgement covers a published checkpoint and every checkpoint
    # installs a floor at its own generation. So classify as if the ack were
    # absent, which is what "this reader has no acknowledgement" means, and
    # keep the floor and checkpoint judgements exactly as strict as they were.
    floor_status, floor = floor_state(vault_root)
    checkpoint_status, checkpoint = checkpoint_state(vault_root)
    epoch = _classify_epoch_states(
        floor_status, floor, checkpoint_status, checkpoint, "absent", None
    )
    if epoch.kind in {"legacy", "pre_floor", "coherent", "recoverable"}:
        return epoch
    raise GraphEpochUnreadable()


def _epoch_writes_with_predecessor(
    vault_root: Path, writes: Iterable[PlannedWrite]
) -> tuple[PlannedWrite, PlannedWrite, GraphSyncCheckpoint | None] | None:
    """Build ordered internal epoch replacements for canonical Markdown writes.

    The import stays here to keep the vault writer free of a module cycle.
    """
    from . import recall_policy
    from .kbdir import kb_dirname
    from .vault import PlannedWrite, content_hash, in_excluded_scan_dir

    # Keep emitted internal writes in the caller's path namespace.  On Windows
    # a caller may legitimately use an 8.3 or case variant while ``resolve``
    # returns the long canonical spelling; mixing the two makes a guarded
    # directory census treat the epoch artifacts as unrelated changes.
    root = Path(vault_root)
    resolved_root = root.resolve()
    paths: list[tuple[str, str | None]] = []
    created_paths: list[str] = []
    for write in writes:
        try:
            relative = write.path.resolve().relative_to(resolved_root).as_posix()
        except (OSError, ValueError):
            continue
        if (
            not relative.endswith(".md")
            or not relative.startswith(f"{kb_dirname()}/")
            or not is_graph_input_path(relative)
            or in_excluded_scan_dir(relative)
            or recall_policy.is_structured_only_path(root, relative)
        ):
            continue
        paths.append((relative, content_hash(write.content)))
        if not write.path.exists():
            created_paths.append(relative)
    if not paths:
        return None
    mutation_id = _checkpoint_mutation_id()
    epoch = _admit_epoch_inputs(root)
    checkpoint = next_checkpoint(
        current=epoch.checkpoint,
        acknowledged_generation=(
            epoch.acknowledgement.generation if epoch.acknowledgement is not None else 0
        ),
        floor_generation=epoch.floor.generation if epoch.floor is not None else 0,
        mutation_id=mutation_id,
        paths=paths,
        created_paths=created_paths,
        force_full_scope=epoch.requires_full_recovery,
    )
    return (
        PlannedWrite(floor_path(root), GraphSyncGenerationFloor.create(checkpoint.generation).render()),
        PlannedWrite(checkpoint_path(root), checkpoint.render()),
        epoch.checkpoint,
    )


def epoch_writes(
    vault_root: Path, writes: Iterable[PlannedWrite]
) -> tuple[PlannedWrite, PlannedWrite] | None:
    """Build ordered internal epoch replacements for canonical Markdown writes."""
    result = _epoch_writes_with_predecessor(vault_root, writes)
    return None if result is None else result[:2]


def deferred_epoch_writes(
    vault_root: Path, writes: Iterable[PlannedWrite]
) -> tuple[PlannedWrite, PlannedWrite, GraphSyncCheckpoint | None] | None:
    """Build one deferred graph token from its admitted predecessor snapshot."""
    return _epoch_writes_with_predecessor(vault_root, writes)


def checkpoint_write(
    vault_root: Path, writes: Iterable[PlannedWrite]
) -> PlannedWrite | None:
    """Compatibility helper for callers that need only the public checkpoint."""
    epoch = epoch_writes(vault_root, writes)
    return None if epoch is None else epoch[1]


def recover_checkpoint(vault_root: Path) -> GraphSyncCheckpoint | None:
    """Publish a full-scope recovery checkpoint without rewriting user Markdown."""
    root = Path(vault_root)
    epoch = _admit_epoch_inputs(root)
    if epoch.kind == "legacy":
        return None
    if epoch.kind == "pre_floor":
        assert epoch.checkpoint is not None
        from .vault import PlannedWrite, batch_atomic_write

        batch_atomic_write(
            [
                PlannedWrite(
                    floor_path(root),
                    GraphSyncGenerationFloor.create(
                        epoch.checkpoint.generation
                    ).render(),
                )
            ],
            vault_root=root,
            post_commit_fanout=False,
            commit_point=False,
        )
        return epoch.checkpoint
    if epoch.kind == "coherent":
        return epoch.checkpoint
    assert epoch.kind == "recoverable"
    checkpoint = next_checkpoint(
        current=epoch.checkpoint,
        acknowledged_generation=(
            epoch.acknowledgement.generation if epoch.acknowledgement is not None else 0
        ),
        floor_generation=epoch.floor.generation if epoch.floor is not None else 0,
        mutation_id=secrets.token_hex(12),
        paths=[],
        created_paths=[],
        force_full_scope=True,
    )
    from .vault import PlannedWrite, batch_atomic_write

    batch_atomic_write(
        [
            PlannedWrite(
                floor_path(root),
                GraphSyncGenerationFloor.create(checkpoint.generation).render(),
            ),
            PlannedWrite(checkpoint_path(root), checkpoint.render()),
        ],
        vault_root=root,
        post_commit_fanout=False,
        commit_point=False,
    )
    return checkpoint


def reconcile_checkpoint(vault_root: Path) -> GraphSyncCheckpoint:
    """Return the exact full-scope checkpoint a graph-only repair will publish."""
    root = Path(vault_root)
    recovered = recover_checkpoint(root)
    if recovered is not None:
        return recovered
    checkpoint = next_checkpoint(
        current=None,
        acknowledged_generation=0,
        mutation_id=_checkpoint_mutation_id(),
        paths=[],
        created_paths=[],
        force_full_scope=True,
    )
    from .vault import PlannedWrite, batch_atomic_write

    batch_atomic_write(
        [
            PlannedWrite(
                floor_path(root),
                GraphSyncGenerationFloor.create(checkpoint.generation).render(),
            ),
            PlannedWrite(checkpoint_path(root), checkpoint.render()),
        ],
        vault_root=root,
        post_commit_fanout=False,
        commit_point=False,
    )
    return checkpoint


def _write_floor(vault_root: Path, floor: GraphSyncGenerationFloor) -> None:
    from .vault import PlannedWrite, batch_atomic_write

    batch_atomic_write(
        [PlannedWrite(floor_path(vault_root), floor.render())],
        vault_root=vault_root,
        post_commit_fanout=False,
        commit_point=False,
    )


def _write_checkpoint(vault_root: Path, checkpoint: GraphSyncCheckpoint) -> None:
    from .vault import PlannedWrite, batch_atomic_write

    batch_atomic_write(
        [PlannedWrite(checkpoint_path(vault_root), checkpoint.render())],
        vault_root=vault_root,
        post_commit_fanout=False,
        commit_point=False,
    )


def _prior_artifact_bytes(path: Path) -> bytes | None:
    try:
        limit = _FLOOR_READ_LIMIT if path.name == _FLOOR_FILENAME else _CHECKPOINT_READ_LIMIT
        return _read_bounded_bytes(path, limit=limit, vault_root=path.parent.parent)
    except FileNotFoundError:
        return None


def _epoch_artifacts_changed(
    vault_root: Path, prior_floor_bytes: bytes | None, prior_checkpoint_bytes: bytes | None
) -> bool:
    """Whether failed epoch staging changed either bounded protocol artifact."""
    return (
        _prior_artifact_bytes(floor_path(vault_root)) != prior_floor_bytes
        or _prior_artifact_bytes(checkpoint_path(vault_root)) != prior_checkpoint_bytes
    )


def _read_bounded_bytes(path: Path, *, limit: int, vault_root: Path | None = None) -> bytes:
    """Read one stable in-vault regular protocol artifact without following links."""
    from .vault import PathGuardError, read_bounded_guarded_bytes

    root = Path(vault_root) if vault_root is not None else Path(path).parent
    try:
        target = Path(path).absolute().relative_to(root.absolute()).as_posix()
    except ValueError as error:
        raise OSError("graph protocol artifact escaped its vault root") from error
    try:
        raw, _guard = read_bounded_guarded_bytes(root, target, limit=limit)
    except PathGuardError as error:
        try:
            Path(path).lstat()
        except FileNotFoundError:
            raise
        raise OSError("graph protocol artifact could not be safely read") from error
    return raw


def _checkpoint_mutation_id() -> str:
    try:
        from .writer_lease import active_mutation_claim_token

        mutation_id = active_mutation_claim_token() or secrets.token_hex(12)
    except Exception:  # noqa: BLE001 - standalone writer fallback
        mutation_id = secrets.token_hex(12)
    return mutation_id if _MUTATION_ID.fullmatch(mutation_id) is not None else secrets.token_hex(12)


def prepare_deletion_epoch(
    vault_root: Path, removed_rel_paths: Iterable[str]
) -> GraphDeletionEpoch | None:
    """Publish deletion floor before the lifecycle rename, retaining rollback proof."""
    from . import recall_policy

    root = Path(vault_root).resolve()
    paths: list[tuple[str, str | None]] = [
        (rel, None)
        for raw in removed_rel_paths
        if (rel := str(raw).replace("\\", "/")).endswith(".md")
        and rel.startswith(f"{checkpoint_path(root).parent.name}/")
        and is_graph_input_path(rel)
        and not recall_policy.is_structured_only_path(root, rel)
        and recall_policy.is_recall_candidate(root, root / rel)
    ]
    if not paths:
        return None
    prior_bytes = _prior_artifact_bytes(floor_path(root))
    prior_checkpoint_bytes = _prior_artifact_bytes(checkpoint_path(root))
    epoch = _admit_epoch_inputs(root)
    checkpoint = next_checkpoint(
        current=epoch.checkpoint,
        acknowledged_generation=(
            epoch.acknowledgement.generation if epoch.acknowledgement is not None else 0
        ),
        floor_generation=epoch.floor.generation if epoch.floor is not None else 0,
        mutation_id=_checkpoint_mutation_id(),
        paths=paths,
        created_paths=[],
        force_full_scope=epoch.requires_full_recovery,
    )
    _write_floor(root, GraphSyncGenerationFloor.create(checkpoint.generation))
    return GraphDeletionEpoch(root, checkpoint, prior_bytes, prior_checkpoint_bytes)


def prepare_recovery_epoch(
    vault_root: Path,
    restored_paths: Iterable[tuple[str, str]],
) -> GraphDeletionEpoch | None:
    """Publish the restore floor before moving graph-relevant Markdown from trash."""
    from . import recall_policy

    root = Path(vault_root).resolve()
    paths: list[tuple[str, str | None]] = [
        (rel, content_hash)
        for raw_rel, raw_content in restored_paths
        if (rel := str(raw_rel).replace("\\", "/")).endswith(".md")
        and rel.startswith(f"{checkpoint_path(root).parent.name}/")
        and is_graph_input_path(rel)
        and not recall_policy.is_structured_only_path(root, rel)
        and isinstance(raw_content, str)
        and (content_hash := raw_content)
    ]
    if not paths:
        return None
    prior_bytes = _prior_artifact_bytes(floor_path(root))
    prior_checkpoint_bytes = _prior_artifact_bytes(checkpoint_path(root))
    epoch = _admit_epoch_inputs(root)
    checkpoint = next_checkpoint(
        current=epoch.checkpoint,
        acknowledged_generation=(
            epoch.acknowledgement.generation if epoch.acknowledgement is not None else 0
        ),
        floor_generation=epoch.floor.generation if epoch.floor is not None else 0,
        mutation_id=_checkpoint_mutation_id(),
        paths=paths,
        created_paths=[rel for rel, _content_hash in paths],
        force_full_scope=epoch.requires_full_recovery,
    )
    _write_floor(root, GraphSyncGenerationFloor.create(checkpoint.generation))
    return GraphDeletionEpoch(root, checkpoint, prior_bytes, prior_checkpoint_bytes)


def commit_deletion_epoch(epoch: GraphDeletionEpoch) -> None:
    """Install the exact checkpoint after the lifecycle rename succeeds."""
    _write_checkpoint(epoch.vault_root, epoch.checkpoint)


def restore_deletion_epoch(epoch: GraphDeletionEpoch) -> None:
    """Restore exact pre-transition floor and checkpoint bytes after a caught failure."""
    _restore_epoch_artifacts(
        epoch.vault_root, epoch.prior_floor_bytes, epoch.prior_checkpoint_bytes
    )


def _restore_epoch_artifacts(
    vault_root: Path, prior_floor_bytes: bytes | None, prior_checkpoint_bytes: bytes | None
) -> None:
    """Restore exact internal epoch bytes without invoking a caller commit point."""
    from .vault import PlannedWrite, batch_atomic_write

    restores = (
        (checkpoint_path(vault_root), prior_checkpoint_bytes),
        (floor_path(vault_root), prior_floor_bytes),
    )
    writes = [
        PlannedWrite(path, raw.decode("utf-8"))
        for path, raw in restores
        if raw is not None
    ]
    if writes:
        batch_atomic_write(
            writes,
            vault_root=vault_root,
            post_commit_fanout=False,
            commit_point=False,
        )
    windows_flushes: list[Path] = []
    for path, raw in restores:
        if raw is None and path.exists():
            path.unlink()
            if os.name == "nt":
                if path.parent not in windows_flushes:
                    windows_flushes.append(path.parent)
                continue
            try:
                directory_fd = os.open(
                    path.parent,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
                )
            except OSError:
                raise
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    if windows_flushes:
        from . import mutation_lock

        for path in windows_flushes:
            mutation_lock._windows_flush_directory(path)


def publish_deletion_checkpoint(
    vault_root: Path, removed_rel_paths: Iterable[str]
) -> GraphSyncCheckpoint | None:
    """Durably record graph-relevant removals before derived fan-out runs."""
    epoch = prepare_deletion_epoch(vault_root, removed_rel_paths)
    if epoch is None:
        return None
    try:
        commit_deletion_epoch(epoch)
    except Exception:
        restore_deletion_epoch(epoch)
        raise
    return epoch.checkpoint


@dataclass(frozen=True)
class GraphBuildOutcome:
    generation: int
    checkpoint_sha256: str

    @classmethod
    def covering(cls, checkpoint: GraphSyncCheckpoint) -> GraphBuildOutcome:
        return cls(checkpoint.generation, checkpoint.checkpoint_sha256)

    def covers(self, checkpoint: GraphSyncCheckpoint) -> bool:
        return self.generation > checkpoint.generation or (
            self.generation == checkpoint.generation
            and self.checkpoint_sha256 == checkpoint.checkpoint_sha256
        )


class GraphRebuildRegistrationError(RuntimeError):
    """A required graph checkpoint has a durable immediate failure handle."""

    def __init__(self, code: str, remediation: str) -> None:
        self.code = code
        self.remediation = remediation
        super().__init__(f"{code}: {remediation}")


class GraphWaiterCapacityError(GraphRebuildRegistrationError):
    """A canonical mutation committed, but this process cannot register another wait."""

    def __init__(self) -> None:
        super().__init__(
            "GRAPH_SYNC_WAITER_CAPACITY",
            f"Retry the same mutation identity, or {_RECONCILE_HINT}",
        )


class GraphRebuildStopped(GraphRebuildRegistrationError):
    """A registered builder exited before producing coverage."""

    def __init__(
        self,
        remediation: str = f"Retry the same mutation identity, or {_RECONCILE_HINT}",
    ) -> None:
        super().__init__(
            "GRAPH_SYNC_REBUILD_STOPPED",
            remediation,
        )


class GraphSidecarReplaceUnavailable(GraphRebuildRegistrationError):
    """Windows has an open reader; retain the old sidecar and recover later."""

    def __init__(self, _message: str = "live graph sidecar has an open reader") -> None:
        super().__init__(
            "GRAPH_SYNC_PLATFORM_SHARING_REFUSED",
            f"Release graph sidecar readers, then {_RECONCILE_HINT}",
        )
        self.args = (f"{_message}: {self.args[0]}",)


class GraphEpochUnreadable(GraphRebuildRegistrationError):
    """The epoch could not be read right now; its history is not in question.

    Deliberately a sibling of `GraphEpochIncoherent` rather than a subclass.
    Every `except GraphEpochIncoherent` in the tree means "this lineage is
    broken" and reaches for a reconcile; inheriting would silently hand all of
    them a case where nothing is broken and the answer is to look again.
    """

    def __init__(self, _message: str = "graph epoch is locked by a concurrent writer") -> None:
        super().__init__(
            "GRAPH_SYNC_EPOCH_BUSY",
            f"Retry the same mutation identity, or {_RECONCILE_HINT}",
        )
        self.args = (f"{_message}: {self.args[0]}",)


class GraphEpochIncoherent(GraphRebuildRegistrationError):
    """Epoch history is ambiguous and must not be repaired by guessing."""

    def __init__(self, _message: str = "graph epoch lineage is incoherent") -> None:
        super().__init__(
            "GRAPH_SYNC_LINEAGE_CONFLICT",
            "Reconcile the graph epoch before retrying this mutation.",
        )
        self.args = (f"{_message}: {self.args[0]}",)


class GraphResetFailed(GraphRebuildRegistrationError):
    """An explicit lineage reset could not safely isolate its derived inputs."""

    def __init__(self, code: str = "GRAPH_SYNC_RESET_REFUSED") -> None:
        super().__init__(code, "Release graph readers or repair the graph lineage before retrying.")


@dataclass(frozen=True)
class GraphReset:
    operation_id: str
    members: tuple[str, ...]
    phase: str


def _reset_directory(vault_root: Path, operation_id: str) -> Path:
    from .vault import kb_root

    return kb_root(vault_root) / f"{_RESET_PREFIX}{operation_id}"


def _reset_manifest_raw(reset: GraphReset, identities: dict[str, tuple[int, ...]]) -> bytes:
    return _canonical_json({
        "version": 1,
        "operation_id": reset.operation_id,
        "members": list(reset.members),
        "identities": {name: list(identities[name]) for name in reset.members},
        "phase": reset.phase,
    })


def _write_reset_manifest(directory: Any, reset: GraphReset, identities: dict[str, tuple[int, ...]]) -> None:
    from .mutation_lock import retain_secure_directory, retained_write_file

    raw = _reset_manifest_raw(reset, identities)
    retained = directory if hasattr(directory, "fd") and hasattr(directory, "path") else retain_secure_directory(directory)
    try:
        retained_write_file(retained, _RESET_MANIFEST, raw)
    finally:
        if retained is not directory:
            retained.close()


def _advance_reset_manifest(directory: Any, reset: GraphReset, identities: dict[str, tuple[int, ...]]) -> None:
    from .mutation_lock import retain_secure_directory, retained_write_file

    raw = _reset_manifest_raw(reset, identities)
    retained = directory if hasattr(directory, "fd") and hasattr(directory, "path") else retain_secure_directory(directory)
    try:
        retained_write_file(retained, _RESET_MANIFEST, raw, replace=True)
    finally:
        if retained is not directory:
            retained.close()


def _parse_reset_manifest(directory: Any, raw: bytes) -> tuple[GraphReset, dict[str, tuple[int, ...]]]:
    """Parse only the closed, content-free reset manifest shape."""
    try:
        value = json.loads(raw)
        operation_id = value["operation_id"]
        members = value["members"]
        identities = value["identities"]
        phase = value["phase"]
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise GraphResetFailed() from error
    if (
        not isinstance(operation_id, str)
        or not re.fullmatch(r"[0-9a-f]{24}", operation_id)
        or Path(directory.path if hasattr(directory, "path") else directory).name != f"{_RESET_PREFIX}{operation_id}"
        or not isinstance(members, list)
        or not members
        or any(not isinstance(name, str) or name not in _RESET_MEMBERS for name in members)
        or len(set(members)) != len(members)
        or phase not in {"prepared", "moving", "isolated"}
        or not isinstance(identities, dict)
    ):
        raise GraphResetFailed()
    parsed: dict[str, tuple[int, ...]] = {}
    for name in members:
        identity = identities.get(name)
        if not isinstance(identity, list) or not identity or any(type(item) is not int for item in identity):
            raise GraphResetFailed()
        parsed[name] = tuple(identity)
    reset = GraphReset(operation_id, tuple(members), phase)
    if raw != _reset_manifest_raw(reset, parsed):
        raise GraphResetFailed()
    return reset, parsed


def _read_reset_manifest(directory: Any) -> tuple[GraphReset, dict[str, tuple[int, ...]]]:
    """Parse only the closed, content-free reset manifest shape."""
    from .mutation_lock import retain_secure_directory, retained_read_file

    retained = directory if hasattr(directory, "fd") and hasattr(directory, "path") else retain_secure_directory(directory)
    try:
        return _parse_reset_manifest(retained, retained_read_file(retained, _RESET_MANIFEST, limit=8_192))
    except OSError as error:
        raise GraphResetFailed() from error
    finally:
        if retained is not directory:
            retained.close()


def _reset_manifest_placement(parent: Any, directory: Any, reset: GraphReset, identities: dict[str, tuple[int, ...]]) -> str:
    """Classify one crash cut from exact retained live/quarantine identities."""
    from .mutation_lock import retain_regular_child_file

    live = quarantined = 0
    for name in reset.members:
        live_item = quarantine_item = None
        try:
            try:
                live_item = retain_regular_child_file(parent, name)
            except FileNotFoundError:
                pass
            try:
                quarantine_item = retain_regular_child_file(directory, name)
            except FileNotFoundError:
                pass
            if live_item is not None and quarantine_item is not None:
                raise GraphResetFailed()
            item = live_item or quarantine_item
            if item is None or item.identity != identities[name]:
                raise GraphResetFailed()
            if live_item is not None:
                live += 1
            else:
                quarantined += 1
        finally:
            if live_item is not None:
                live_item.close()
            if quarantine_item is not None:
                quarantine_item.close()
    if live == len(reset.members):
        return "prepared"
    if quarantined == len(reset.members):
        return "isolated"
    return "moving"


def _resolve_reset_manifest_residue(parent: Any, directory: Any) -> tuple[GraphReset, dict[str, tuple[int, ...]]]:
    """Resolve a crash-left staging/current transition from object identities, never phase text."""
    from .mutation_lock import retained_read_file, retained_unlink_file, retained_write_file

    try:
        current_raw = retained_read_file(directory, _RESET_MANIFEST, limit=8_192)
    except FileNotFoundError:
        current_raw = None
    try:
        staged_raw = retained_read_file(directory, f".{_RESET_MANIFEST}.new", limit=8_192)
    except FileNotFoundError:
        staged_raw = None
    if staged_raw is None:
        if current_raw is None:
            raise GraphResetFailed()
        return _parse_reset_manifest(directory, current_raw)
    staged, staged_identities = _parse_reset_manifest(directory, staged_raw)
    if current_raw is not None:
        current, current_identities = _parse_reset_manifest(directory, current_raw)
        if (
            current.operation_id != staged.operation_id
            or current.members != staged.members
            or current_identities != staged_identities
        ):
            raise GraphResetFailed()
    placement = _reset_manifest_placement(parent, directory, staged, staged_identities)
    adopted = GraphReset(staged.operation_id, staged.members, placement)
    # The selected phase is derived from placement, so a cut before or after
    # either manifest publish makes the same deterministic next decision.
    retained_unlink_file(directory, f".{_RESET_MANIFEST}.new")
    retained_write_file(directory, _RESET_MANIFEST, _reset_manifest_raw(adopted, staged_identities), replace=current_raw is not None)
    return adopted, staged_identities


def _recover_interrupted_reset(vault_root: Path) -> GraphReset | None:
    """Reverse a partial transaction only when its retained identities prove it safe."""
    from .mutation_lock import (
        rename_retained_regular_file,
        retain_child_directory,
        retain_regular_child_file,
        retain_secure_directory,
    )
    kb = _reset_directory(vault_root, "0" * 24).parent
    parent = retain_secure_directory(kb)
    try:
        entries = os.listdir(parent.fd) if parent.fd is not None else os.listdir(parent.path)
        candidates: list[tuple[Path, Any]] = []
        for name in entries:
            if not name.startswith(_RESET_PREFIX):
                continue
            candidate = kb / name
            held = retain_child_directory(parent, name, delete_access=True)
            candidates.append((candidate, held))
    except BaseException:
        parent.close()
        raise
    if not candidates:
        parent.close()
        return None
    if len(candidates) != 1:
        for _candidate, held in candidates:
            held.close()
        parent.close()
        raise GraphResetFailed()
    directory, retained_directory = candidates[0]
    try:
        reset, identities = _resolve_reset_manifest_residue(parent, retained_directory)
        if reset.phase == "isolated":
            if not _isolated_reset_matches(retained_directory, parent, reset.members, identities):
                raise GraphResetFailed()
            return reset
        for name in reset.members:
            try:
                retained = retain_regular_child_file(retained_directory, name)
            except FileNotFoundError:
                retained = None
            if retained is not None:
                try:
                    if retained.identity != identities[name]:
                        raise GraphResetFailed()
                    rename_retained_regular_file(retained, kb / name, destination_directory=parent)
                finally:
                    retained.close()
            else:
                try:
                    retained = retain_regular_child_file(parent, name)
                except FileNotFoundError as error:
                    raise GraphResetFailed() from error
                try:
                    if retained.identity != identities[name]:
                        raise GraphResetFailed()
                finally:
                    retained.close()
        _advance_reset_manifest(retained_directory, GraphReset(reset.operation_id, reset.members, "prepared"), identities)
        if not _cleanup_graph_lineage_reset_retained(parent, retained_directory, reset):
            raise GraphResetFailed("GRAPH_SYNC_RESET_ROLLBACK_FAILED")
        retained_directory = None
        return None
    finally:
        if retained_directory is not None:
            retained_directory.close()
        parent.close()


def recover_isolated_graph_lineage_reset(vault_root: Path) -> GraphReset | None:
    """Adopt one durable isolated reset on an explicit recovery invocation."""
    reset = _recover_interrupted_reset(vault_root)
    if reset is not None and reset.phase != "isolated":
        raise GraphResetFailed()
    return reset


def census_unavailable_graph_lineage(vault_root: Path) -> tuple[str, ...]:
    """Return the exact safe live derived set without moving or registering work."""
    from .mutation_lock import (
        retain_regular_child_file,
        retain_secure_directory,
        retained_regular_child_names,
    )
    from .writer_lease import active_manager

    root = Path(vault_root)
    coordinator = active_manager()._mutation_coordinator_for(root)
    with coordinator.hold(operation="graph_lineage_reset_census", holder_kind="graph"):
        if classify_epoch(root).kind != "unavailable":
            return ()
        kb = _reset_directory(root, "0" * 24).parent
        parent = retain_secure_directory(kb)
        retained = []
        try:
            members = retained_regular_child_names(parent, _RESET_MEMBERS)
            if not members:
                raise GraphResetFailed()
            for name in members:
                retained.append(retain_regular_child_file(parent, name))
            return members
        finally:
            for item in retained:
                item.close()
            parent.close()


def _isolated_reset_matches(
    directory: Any, parent: Any, members: tuple[str, ...], identities: dict[str, tuple[int, ...]]
) -> bool:
    from .mutation_lock import retain_regular_child_file, retained_regular_child_names

    try:
        if retained_regular_child_names(parent, _RESET_MEMBERS):
            return False
        for name in members:
            held = retain_regular_child_file(directory, name)
            try:
                if held.identity != identities[name]:
                    return False
            finally:
                held.close()
        return True
    except OSError:
        return False


def isolate_unavailable_graph_lineage(vault_root: Path) -> GraphReset | None:
    """Durably quarantine only the exact derived graph set for an unavailable epoch."""
    from .mutation_lock import rename_retained_regular_file, retain_regular_file
    from .writer_lease import active_manager

    root = Path(vault_root)
    coordinator = active_manager()._mutation_coordinator_for(root)
    with coordinator.hold(operation="graph_lineage_reset", holder_kind="graph"):
        if classify_epoch(root).kind != "unavailable":
            return None
        interrupted = _recover_interrupted_reset(root)
        if interrupted is not None:
            return interrupted
        kb = _reset_directory(root, "0" * 24).parent
        members = census_unavailable_graph_lineage(root)
        retained = []
        try:
            for name in members:
                retained.append(retain_regular_file(kb / name))
            identities = {item.path.name: item.identity for item in retained}
            operation_id = secrets.token_hex(12)
            from .mutation_lock import create_retained_child_directory, retain_secure_directory

            parent = retain_secure_directory(kb)
            try:
                created = create_retained_child_directory(parent, f"{_RESET_PREFIX}{operation_id}")
                directory = created.path
                reset = GraphReset(operation_id, members, "prepared")
                _write_reset_manifest(created, reset, identities)
                moved = []
                try:
                    _advance_reset_manifest(created, GraphReset(operation_id, members, "moving"), identities)
                    for item in retained:
                        moved.append(item)
                        rename_retained_regular_file(item, directory / item.path.name, destination_directory=created)
                        _advance_reset_manifest(created, GraphReset(operation_id, members, "moving"), identities)
                    _advance_reset_manifest(created, GraphReset(operation_id, members, "isolated"), identities)
                    if not _isolated_reset_matches(created, parent, members, identities):
                        raise GraphResetFailed()
                    return GraphReset(operation_id, members, "isolated")
                except Exception as error:
                    rollback_failed = False
                    for item in reversed(moved):
                        try:
                            target = directory / item.path.name
                            if not target.exists():
                                if (kb / item.path.name).exists():
                                    continue
                                raise FileNotFoundError(item.path.name)
                            moved_item = retain_regular_file(target)
                            try:
                                rename_retained_regular_file(moved_item, kb / item.path.name, destination_directory=parent)
                            finally:
                                moved_item.close()
                        except Exception:  # noqa: BLE001 - rollback must absorb any cleanup failure
                            rollback_failed = True
                    raise GraphResetFailed("GRAPH_SYNC_RESET_ROLLBACK_FAILED" if rollback_failed else "GRAPH_SYNC_RESET_REFUSED") from error
            finally:
                parent.close()
                if 'created' in locals():
                    created.close()
        finally:
            for item in retained:
                item.close()


def _cleanup_graph_lineage_reset_retained(parent: Any, directory: Any, reset: GraphReset) -> bool:
    """Remove one proven rollback quarantine while both parent handles remain pinned."""
    from .mutation_lock import remove_retained_child_directory, retained_unlink_file

    try:
        for name in reset.members:
            try:
                retained_unlink_file(directory, name)
            except FileNotFoundError:
                pass
        retained_unlink_file(directory, _RESET_MANIFEST)
        try:
            retained_unlink_file(directory, f".{_RESET_MANIFEST}.new")
        except FileNotFoundError:
            pass
        remove_retained_child_directory(parent, directory, directory.path.name)
        return True
    except OSError:
        return False


def cleanup_graph_lineage_reset(vault_root: Path, operation_id: str) -> bool:
    """Best-effort, non-recursive cleanup of one manifest-recorded quarantine."""
    from .mutation_lock import retain_child_directory, retain_secure_directory

    kb = _reset_directory(Path(vault_root), operation_id).parent
    name = f"{_RESET_PREFIX}{operation_id}"
    try:
        parent = retain_secure_directory(kb)
        try:
            directory: _SecureDirectory | None = retain_child_directory(
                parent, name, delete_access=True
            )
            try:
                reset, _identities = _resolve_reset_manifest_residue(parent, directory)
                if reset.phase != "prepared":
                    return False
                cleaned = _cleanup_graph_lineage_reset_retained(parent, directory, reset)
                directory = None
                return cleaned
            finally:
                if directory is not None:
                    directory.close()
        finally:
            parent.close()
    except OSError:
        return False


def cleanup_published_graph_lineage_reset(
    vault_root: Path, operation_id: str, checkpoint: GraphSyncCheckpoint
) -> bool:
    """Remove one isolated reset only after its registered checkpoint is current."""
    root = Path(vault_root)
    try:
        observed = read_checkpoint(root)
        if (
            status(root)["state"] != "current"
            or observed is None
            or observed.generation < checkpoint.generation
            or (
                observed.generation == checkpoint.generation
                and observed.checkpoint_sha256 != checkpoint.checkpoint_sha256
            )
        ):
            return False
        from .mutation_lock import retain_child_directory, retain_secure_directory

        kb = _reset_directory(root, operation_id).parent
        parent = retain_secure_directory(kb)
        try:
            directory: _SecureDirectory | None = retain_child_directory(
                parent, f"{_RESET_PREFIX}{operation_id}", delete_access=True
            )
            try:
                reset, _identities = _resolve_reset_manifest_residue(parent, directory)
                if reset.phase != "isolated":
                    return False
                cleaned = _cleanup_graph_lineage_reset_retained(parent, directory, reset)
                directory = None
                return cleaned
            finally:
                if directory is not None:
                    directory.close()
        finally:
            parent.close()
    except (OSError, GraphResetFailed):
        return False


class GraphRebuildLockUnavailable(GraphRebuildRegistrationError):
    """Secure rebuild-lock setup failed before an owner could be determined."""

    def __init__(self, _message: str = "secure graph rebuild lock could not be established") -> None:
        super().__init__(
            "GRAPH_SYNC_REBUILD_LOCK_UNAVAILABLE",
            "Retry the same mutation identity after graph rebuild locking recovers, or run reconcile.",
        )
        self.args = (f"{_message}: {self.args[0]}",)


class GraphRebuildWaiter:
    def __init__(
        self,
        coordinator: GraphRebuildCoordinator,
        checkpoint: GraphSyncCheckpoint,
        immediate_error: BaseException | None = None,
        admitted: bool = True,
    ):
        self._coordinator = coordinator
        self._checkpoint = checkpoint
        self._immediate_error = immediate_error
        self._admitted = admitted
        self.builder_started = False
        self._released = False

    def wait(self, timeout: float | None = None) -> GraphBuildOutcome:
        try:
            if self._immediate_error is not None:
                raise self._immediate_error
            return self._coordinator._wait(self._checkpoint, timeout)
        finally:
            if self._admitted and not self._released:
                self._released = True
                self._coordinator._release_waiter()


@dataclass(frozen=True)
class GraphRebuildStart:
    """A capacity-neutral result from starting or coalescing a flight."""

    builder_started: bool


class GraphRebuildRegistration:
    """Exact rebuild work captured under canonical authority, but not started."""

    def __init__(
        self,
        coordinator: GraphRebuildCoordinator,
        checkpoint: GraphSyncCheckpoint,
        builder: Callable[[GraphSyncCheckpoint], GraphBuildOutcome],
    ) -> None:
        self._coordinator = coordinator
        self._checkpoint = checkpoint
        self._builder = builder

    def start(self) -> GraphRebuildStart:
        return self._coordinator.ensure_started(self._checkpoint, self._builder)

    def wait(self, timeout: float | None = None) -> GraphBuildOutcome:
        self.start()
        return self._coordinator.join(self._checkpoint).wait(timeout)


class GraphRebuildCoordinator:
    """One process-local flight per vault; canonical writers never wait in it."""

    def __init__(self, vault_root: Path):
        self.vault_root = Path(vault_root).resolve()
        self._condition = threading.Condition()
        self._required: GraphSyncCheckpoint | None = None
        self._outcome: GraphBuildOutcome | None = None
        self._error: BaseException | None = None
        self._running = False
        self._builder: Callable[[GraphSyncCheckpoint], GraphBuildOutcome] | None = None
        self._waiter_count = 0

    @property
    def writer_hold_count(self) -> int:
        return 0

    @property
    def waiter_count(self) -> int:
        with self._condition:
            return self._waiter_count

    def ensure_started(
        self,
        checkpoint: GraphSyncCheckpoint,
        builder: Callable[[GraphSyncCheckpoint], GraphBuildOutcome],
    ) -> GraphRebuildStart:
        """Start or coalesce exact work without admitting a response waiter."""
        with self._condition:
            if (
                self._required is not None
                and checkpoint.generation == self._required.generation
                and checkpoint.checkpoint_sha256 != self._required.checkpoint_sha256
            ):
                self._error = GraphEpochIncoherent(
                    "same-generation graph checkpoints have different lineage"
                )
                self._condition.notify_all()
                return GraphRebuildStart(False)
            if self._required is None or checkpoint.generation > self._required.generation:
                self._required = checkpoint
            if self._running:
                self._condition.notify_all()
                return GraphRebuildStart(False)
            if self._outcome is not None and self._outcome.covers(checkpoint):
                return GraphRebuildStart(False)
            self._running = True
            self._error = None
            self._outcome = None
            self._builder = builder
            try:
                threading.Thread(
                    target=self._run,
                    name="exomem-graph-rebuild",
                    daemon=True,
                ).start()
            except RuntimeError:
                self._running = False
                self._error = GraphRebuildRegistrationError(
                    "GRAPH_SYNC_START_FAILED",
                    f"Retry the same mutation identity, or {_RECONCILE_HINT}",
                )
                self._condition.notify_all()
                return GraphRebuildStart(False)
            self._condition.notify_all()
            return GraphRebuildStart(True)

    def join(self, checkpoint: GraphSyncCheckpoint) -> GraphRebuildWaiter:
        """Admit one bounded response waiter for work already registered."""
        with self._condition:
            if self._waiter_count >= MAX_GRAPH_REBUILD_WAITERS:
                return GraphRebuildWaiter(
                    self,
                    checkpoint,
                    GraphWaiterCapacityError(),
                    admitted=False,
                )
            self._waiter_count += 1
            return GraphRebuildWaiter(self, checkpoint)

    def start_or_join(
        self,
        checkpoint: GraphSyncCheckpoint,
        builder: Callable[[GraphSyncCheckpoint], GraphBuildOutcome],
    ) -> GraphRebuildWaiter:
        started = self.ensure_started(checkpoint, builder)
        waiter = self.join(checkpoint)
        waiter.builder_started = started.builder_started
        return waiter

    def _release_waiter(self) -> None:
        with self._condition:
            self._waiter_count = max(0, self._waiter_count - 1)

    def _run(self) -> None:
        attempts = 0
        while attempts < MAX_GRAPH_REBUILD_ATTEMPTS:
            attempts += 1
            with self._condition:
                assert self._required is not None
                assert self._builder is not None
                required = self._required
                builder = self._builder
            try:
                outcome = builder(required)
            except BaseException as error:  # noqa: BLE001 - integration path
                if isinstance(error, GraphRebuildRegistrationError):
                    projection = error
                    if isinstance(error, GraphEpochIncoherent):
                        try:
                            state = status(self.vault_root)["state"]
                        except Exception:  # noqa: BLE001 - status is fail-closed
                            pass
                        else:
                            if state == "unavailable":
                                projection = GraphRebuildRegistrationError(
                                    error.code,
                                    "Run maintain_memory(mode=\"reconcile\", dry_run=false, "
                                    "rebuild_graph=true) to recover the derived graph.",
                                )
                                projection.__cause__ = error
                else:
                    try:
                        state = status(self.vault_root)["state"]
                    except Exception:  # noqa: BLE001 - status is fail-closed
                        remediation = _RECONCILE_REMEDIATION
                    else:
                        remediation = (
                            "Run maintain_memory(mode=\"reconcile\", dry_run=false, rebuild_graph=true) "
                            "to recover the derived graph."
                            if state == "unavailable"
                            else f"Retry the same mutation identity, or {_RECONCILE_HINT}"
                        )
                    projection = GraphRebuildStopped(remediation)
                    projection.__cause__ = error
                logger.exception(
                    "graph rebuild stopped checkpoint_sha256=%s generation=%s",
                    required.checkpoint_sha256,
                    required.generation,
                )
                with self._condition:
                    self._error = projection
                    self._running = False
                    self._condition.notify_all()
                return
            with self._condition:
                if self._error is not None:
                    self._running = False
                    self._condition.notify_all()
                    return
                if self._required is not None and not outcome.covers(self._required):
                    continue
                self._outcome = outcome
                self._running = False
                self._condition.notify_all()
                return
        with self._condition:
            self._error = GraphRebuildRegistrationError(
                "GRAPH_SYNC_STABILIZATION_EXHAUSTED",
                "Run reconcile to recover the derived graph.",
            )
            self._running = False
            self._condition.notify_all()

    def _wait(self, checkpoint: GraphSyncCheckpoint, timeout: float | None) -> GraphBuildOutcome:
        with self._condition:
            ready = self._condition.wait_for(
                lambda: self._error is not None
                or (self._outcome is not None and self._outcome.covers(checkpoint)),
                timeout=timeout,
            )
            if not ready:
                raise TimeoutError("graph rebuild did not finish before the wait deadline")
            if self._error is not None:
                raise self._error
            assert self._outcome is not None
            return self._outcome


def _registration_runtime_root(state_root: Path | None) -> Path:
    return _rebuild_runtime_root(state_root)


def _registration_key(vault_root: Path, state_root: Path | None) -> str:
    return _rebuild_lock_key(vault_root, _registration_runtime_root(state_root))


def register_rebuild(
    vault_root: Path,
    checkpoint: GraphSyncCheckpoint,
    builder: Callable[[GraphSyncCheckpoint], GraphBuildOutcome],
    *,
    state_root: Path | None = None,
) -> GraphRebuildRegistration:
    """Capture exact rebuild work; callers start or join only after their guard exits."""
    key = _registration_key(vault_root, state_root)
    with _COORDINATORS_LOCK:
        coordinator = _COORDINATORS.setdefault(key, GraphRebuildCoordinator(Path(vault_root)))
    registration = GraphRebuildRegistration(coordinator, checkpoint, builder)
    pending = dict(_PENDING_WAITERS.get() or {})
    pending[key] = (registration, checkpoint)
    _PENDING_WAITERS.set(pending)
    return registration


def register_deferred(
    vault_root: Path, checkpoint: GraphSyncCheckpoint, *, state_root: Path | None = None
) -> GraphRebuildWaiter:
    """Bind required work for rollback mode without starting a scheduler thread."""
    key = _registration_key(vault_root, state_root)
    waiter = GraphRebuildWaiter(
        GraphRebuildCoordinator(Path(vault_root)),
        checkpoint,
        GraphRebuildRegistrationError(
            "GRAPH_SYNC_SCHEDULING_DISABLED",
            f"Enable graph scheduling, or {_RECONCILE_HINT}",
        ),
        admitted=False,
    )
    pending = dict(_PENDING_WAITERS.get() or {})
    pending[key] = (waiter, checkpoint)
    _PENDING_WAITERS.set(pending)
    return waiter


def register_failure(
    vault_root: Path,
    checkpoint: GraphSyncCheckpoint,
    *,
    code: str,
    remediation: str = "Run reconcile to recover the derived graph.",
    state_root: Path | None = None,
) -> GraphRebuildWaiter:
    """Install an exact immediate-error handle when lazy work cannot register.

    This is deliberately the same context-bound handoff used by normal rebuild
    registrations, so the idempotency join sees one exact checkpoint rather
    than silently treating the canonical mutation as graph-current.
    """
    if not code or not code.isascii() or len(code) > 64:
        raise ValueError("graph failure code must be bounded ASCII")
    key = _registration_key(vault_root, state_root)
    waiter = GraphRebuildWaiter(
        GraphRebuildCoordinator(Path(vault_root)),
        checkpoint,
        GraphRebuildRegistrationError(code, remediation),
        admitted=False,
    )
    pending = dict(_PENDING_WAITERS.get() or {})
    pending[key] = (waiter, checkpoint)
    _PENDING_WAITERS.set(pending)
    return waiter


def register_outer_fanout_failure(
    vault_root: Path,
    *,
    code: str = "GRAPH_SYNC_FANOUT_FAILED",
    remediation: str = "Run reconcile to recover the derived graph.",
) -> GraphSyncCheckpoint | None:
    """Bind a caught post-checkpoint fanout error to its exact graph epoch.

    The caller's canonical transition has already committed at this point.
    Replacing any incomplete handoff with an immediate error handle makes that
    committed-derived failure visible to the active lease and its retry receipt.
    """
    checkpoint = read_checkpoint(vault_root)
    if checkpoint is None:
        return None
    register_failure(vault_root, checkpoint, code=code, remediation=remediation)
    return checkpoint


def wait_for_registered(
    vault_root: Path, timeout: float | None = None, *, state_root: Path | None = None
) -> GraphBuildOutcome | None:
    key = _registration_key(vault_root, state_root)
    pending = dict(_PENDING_WAITERS.get() or {})
    item = pending.pop(key, None)
    _PENDING_WAITERS.set(pending or None)
    return None if item is None else item[0].wait(timeout)


#: The one thread name every graph rebuild runs under, in both the registered
#: path (`GraphRebuildCoordinator.ensure_started`) and the warming path
#: (`epistemic_graph.schedule_background_rebuild`).
GRAPH_REBUILD_THREAD_NAME = "exomem-graph-rebuild"

#: A rebuild over a realistic vault is seconds; a whole-vault pass over a large
#: one has been measured in the low minutes. Long enough that a healthy rebuild
#: always finishes, short enough that a wedged one cannot hold a shell prompt
#: open indefinitely -- at which point the checkpoint is still durable and the
#: next run or a reconcile repairs it.
_EXIT_DRAIN_SECONDS = 300.0


def drain_active_rebuilds(timeout: float = _EXIT_DRAIN_SECONDS) -> bool:
    """Let in-flight graph rebuilds finish before this process ends.

    Taking the rebuild off the *write* path is correct: a request should not pay
    for a whole-vault build. Letting the *process* exit with that build still
    running is not, and the two are easy to conflate.

    A rebuild runs on a daemon thread, which is right for the long-lived server
    -- the request returns, the rebuild lands moments later, and a reader that
    polls sees it converge. It is wrong for a one-shot CLI process, which exits
    as soon as the command returns and takes the daemon with it. The write would
    report `pending` and nothing would ever make it true: every `exomem`
    invocation would leave the graph a little further behind, and only an
    explicit reconcile would catch up.

    So the boundary is process lifetime, not the write path. Returns whether
    everything drained, so a caller can say so rather than exit silently on a
    rebuild it abandoned.
    Reads the clock only once it has something to wait for. That is not a
    micro-optimization: this runs from an autouse teardown fixture on every test
    in the suite, and a test is entitled to replace `time.monotonic` with a
    scripted sequence for its own purposes. Charging the empty case a clock read
    made one such test fail in teardown with `generator raised StopIteration`,
    from a fixture that had nothing to drain.
    """
    deadline: float | None = None
    while True:
        alive = [
            thread
            for thread in threading.enumerate()
            if thread.name == GRAPH_REBUILD_THREAD_NAME and thread.is_alive()
        ]
        if not alive:
            return True
        if deadline is None:
            deadline = time.monotonic() + timeout
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        for thread in alive:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))


def await_active_rebuild(
    vault_root: Path,
    *,
    state_root: Path | None = None,
    timeout: float | None = None,
) -> GraphBuildOutcome | None:
    """Wait for this vault's in-flight rebuild to settle, outside the request seam.

    `wait_for_registered` consumes a *per-request* registration: once an
    interactive write has polled it and left (#576/#588), the registration is
    gone even though the flight is still running on its own thread. A caller
    that genuinely needs convergence — an operator drain, a test asserting the
    graph's own outcome — has nothing left to join.

    This joins the flight itself. Returns the outcome, re-raises the builder's
    exact failure (so a real rebuild failure is never laundered into "nothing
    happened"), or returns None when no flight is in progress.

    It is deliberately NOT reachable from the write path: nothing in
    `writer_lease` may call it, and `test_bounded_graph_join.py` enumerates the
    join sites that could.
    """
    key = _registration_key(vault_root, state_root)
    with _COORDINATORS_LOCK:
        coordinator = _COORDINATORS.get(key)
    if coordinator is None:
        return None
    with coordinator._condition:
        required = coordinator._required
        idle = not coordinator._running and coordinator._error is None
    if required is None or (idle and coordinator._outcome is None):
        return None
    return coordinator._wait(required, timeout)


def join_registered_if_settled(
    vault_root: Path, *, state_root: Path | None = None
) -> bool:
    """Take a registered rebuild's outcome if it has one; never block for it.

    The single seam every request-serving join goes through (#576/#588), rather
    than a timeout threaded through each call site by hand. The first analysis
    of this incident bounded one site and left an identical unbounded join in
    `mutation_guard` that then produced the worst case measured -- a 300 s
    single-unit append. One named helper makes the set greppable, and
    `test_bounded_graph_join.py` fails on any new `wait_for_registered` caller
    that is neither this helper nor a declared opt-out.

    Returns True when the flight has already converged, and False when it is
    still running -- in which case the canonical bytes are durable, the flight
    keeps going on its own thread, and the caller owes its own caller an honest
    "derived graph is still catching up". Every other failure still raises, so
    a real rebuild failure is not laundered into "still pending".

    `Condition.wait_for` with `timeout=0` evaluates its predicate exactly once
    and returns, so this costs one lock acquisition and no scheduling.

    Deliberately not the default of `wait_for_registered`: `reconcile`, whose
    terminal exists to prove the graph is readable, must keep blocking, and a
    default that silently changed under it would be the same class of bug.
    """
    try:
        wait_for_registered(
            vault_root,
            _SETTLED_JOIN_TIMEOUT_SECONDS,
            state_root=state_root,
        )
    except TimeoutError:
        return False
    return True


def start_registered(vault_root: Path, *, state_root: Path | None = None) -> GraphRebuildStart | None:
    """Start captured work without joining it, for standalone post-commit fanout."""
    item = (_PENDING_WAITERS.get() or {}).get(_registration_key(vault_root, state_root))
    if item is None or not isinstance(item[0], GraphRebuildRegistration):
        return None
    return item[0].start()


def registered_checkpoint(
    vault_root: Path, *, state_root: Path | None = None
) -> GraphSyncCheckpoint | None:
    item = (_PENDING_WAITERS.get() or {}).get(_registration_key(vault_root, state_root))
    return item[1] if item is not None else None


def temporary_sidecar_path(live: Path, checkpoint: GraphSyncCheckpoint) -> Path:
    # The checkpoint digest identifies the intended publication, not one
    # process. A per-attempt nonce prevents two replicas from sharing a temp
    # SQLite inode before cross-process ownership has converged.
    return live.with_name(
        f"{_TEMP_PREFIX}{checkpoint.checkpoint_sha256}-{secrets.token_hex(12)}.sqlite"
    )


PUBLISH_IN_PLACE_ATTEMPTS = 3
PUBLISH_IN_PLACE_BUSY_TIMEOUT_SECONDS = 5.0
PUBLISH_IN_PLACE_RETRY_SECONDS = 0.25


def replace_sidecar(temporary: Path, live: Path) -> None:
    """Publish the proven temporary as the live sidecar.

    The fast path is the platform's atomic replacement primitive, which Linux
    always grants and which Windows grants once no handle is open on the
    destination. Windows refuses it while any reader keeps the live SQLite file
    open, and a resident service makes that a routine condition rather than a
    rare one, so a refusal must not be the end of the road: fall back to
    publishing the same proven bytes *into* the existing file with SQLite's
    backup API, which leaves the directory entry untouched and so does not care
    how many handles are open on it.

    That fallback covers open *handles*, not open read *transactions*. This
    sidecar runs in rollback-journal mode, where a reader inside `BEGIN` holds a
    SHARED lock that blocks the backup's EXCLUSIVE one — so a reader that holds
    a transaction across the whole window still defeats both paths. Bounded
    retries absorb the ordinary case where the reader commits and lets go.

    Only when both fail is the publication genuinely unavailable. Leaving the
    complete old sidecar in place is fail-closed; the checkpoint remains
    unacknowledged and the graph's own recovery path retries later.
    """
    try:
        os.replace(temporary, live)
    except PermissionError as error:
        if _publish_sidecar_in_place(temporary, live):
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                # A retained temp is inert once the live file already carries
                # the published bytes; the reaper collects it on a later pass.
                pass
            return
        raise GraphSidecarReplaceUnavailable("live graph sidecar has an open reader") from error


def _publish_sidecar_in_place(temporary: Path, live: Path) -> bool:
    """Copy a proven temp sidecar over the live file without moving the entry.

    `sqlite3.Connection.backup` copies the whole source database inside one
    destination transaction, so a concurrent reader either sees the complete
    old content or the complete new content — never a half-written file.

    The sidecar is a rollback-journal database (nothing sets `journal_mode`), so
    a reader inside `BEGIN` holds a SHARED lock and the backup's EXCLUSIVE lock
    waits on it. The busy timeout absorbs that once the connection is open; a
    failure raised at *open* time returns immediately, so the attempts are also
    spaced, or all three would burn in microseconds and prove nothing. A reader
    that never yields exhausts them and leaves the live sidecar exactly as it
    was.
    """
    if not live.exists():
        return False
    for attempt in range(PUBLISH_IN_PLACE_ATTEMPTS):
        if attempt:
            time.sleep(PUBLISH_IN_PLACE_RETRY_SECONDS)
        try:
            source = sqlite3.connect(temporary)
            try:
                destination = sqlite3.connect(
                    live, timeout=PUBLISH_IN_PLACE_BUSY_TIMEOUT_SECONDS
                )
                try:
                    source.backup(destination)
                finally:
                    destination.close()
            finally:
                source.close()
        except sqlite3.Error:
            logger.debug(
                "graph sidecar in-place publication attempt %d/%d did not complete",
                attempt + 1,
                PUBLISH_IN_PLACE_ATTEMPTS,
                exc_info=True,
            )
            continue
        except OSError:
            return False
        return True
    return False


def _rebuild_runtime_root(state_root: Path | None) -> Path:
    if state_root is not None:
        return Path(state_root).expanduser()
    from .writer_lease import active_manager

    return active_manager().config.state_dir


def _rebuild_lock_path(vault_root: Path, *, state_root: Path | None = None) -> Path:
    """Return the persistent runtime-state lock for one canonical vault."""
    from .mutation_lock import canonical_mutation_identity

    identity = canonical_mutation_identity(vault_root).encode("utf-8")
    return (
        _rebuild_runtime_root(state_root)
        / f"{hashlib.sha256(identity).hexdigest()}.graph-rebuild.lock"
    )


def _rebuild_lock_key(vault_root: Path, state_root: Path) -> str:
    return f"{Path(vault_root).resolve()}\0{Path(state_root).expanduser().resolve(strict=False)}"


@dataclass
class _HeldRebuildLock:
    """Explicit ownership of the rebuild descriptor and secure namespace."""

    handle: BinaryIO | None
    directory: Any | None
    release_os_lock: Callable[[BinaryIO], None]
    _close_guard: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def close(self, *, release_os_lock: bool = True) -> None:
        """Release this process's lock and namespace handles exactly once."""
        with self._close_guard:
            handle = self.handle
            directory = self.directory
            self.handle = None
            self.directory = None
        if handle is None:
            return
        try:
            if release_os_lock:
                self.release_os_lock(handle)
        finally:
            try:
                handle.close()
            finally:
                if directory is not None:
                    directory.close()

    def close_after_fork(self) -> None:
        """Drop inherited descriptors without touching the parent's flock."""
        # Do not acquire the inherited thread lock here: a non-forking parent
        # thread may have owned it when fork occurred. Closing duplicate file
        # descriptors cannot release the parent's open-file-description lock.
        handle = self.handle
        directory = self.directory
        self.handle = None
        self.directory = None
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass
        if directory is not None:
            try:
                directory.close()
            except OSError:
                pass


def claim_rebuild_owner(
    vault_root: Path, temporary: Path, *, state_root: Path | None = None
) -> bool:
    """Acquire the descriptor-bound cross-process private-build lock."""
    del temporary
    from .mutation_lock import (
        _acquire_trusted_runtime_root,
        _open_owned_runtime_lock_file,
        _release_os_lock,
        _same_directory_path,
        _same_file_entry,
        _try_os_lock,
    )

    runtime_root = _rebuild_runtime_root(state_root)
    key = _rebuild_lock_key(vault_root, runtime_root)
    with _COORDINATORS_LOCK:
        if key in _REBUILD_LOCK_HANDLES:
            return False
    lock = _rebuild_lock_path(vault_root, state_root=runtime_root)
    directory: Any = None
    directory_transferred = False
    handle: BinaryIO | None = None
    locked = False
    try:
        directory = _acquire_trusted_runtime_root(lock.parent)
        descriptor = _open_owned_runtime_lock_file(directory, lock.name)
        handle = os.fdopen(descriptor, "a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if not _same_directory_path(directory) or not _same_file_entry(
            directory, lock.name, handle.fileno()
        ):
            raise OSError("rebuild lock namespace changed before acquisition")
        if not _try_os_lock(handle):
            return False
        locked = True
        if not _same_directory_path(directory) or not _same_file_entry(
            directory, lock.name, handle.fileno()
        ):
            raise OSError("rebuild lock namespace changed during acquisition")
        with _COORDINATORS_LOCK:
            if key in _REBUILD_LOCK_HANDLES:
                _release_os_lock(handle)
                locked = False
                return False
            _REBUILD_LOCK_HANDLES[key] = _HeldRebuildLock(
                handle,
                directory,
                _release_os_lock,
            )
            handle = None
            directory_transferred = True
        return True
    except OSError:
        raise GraphRebuildLockUnavailable(
            "secure graph rebuild lock could not be established"
        ) from None
    finally:
        if handle is not None:
            if locked:
                try:
                    _release_os_lock(handle)
                except OSError:
                    pass
            handle.close()
        if directory is not None and not directory_transferred:
            directory.close()


def release_rebuild_owner(
    vault_root: Path, temporary: Path, *, state_root: Path | None = None
) -> None:
    """Release a held runtime-state lock without unlinking its authority path."""
    del temporary
    runtime_root = _rebuild_runtime_root(state_root)
    with _COORDINATORS_LOCK:
        held = _REBUILD_LOCK_HANDLES.pop(_rebuild_lock_key(vault_root, runtime_root), None)
    if held is None:
        return
    held.close()


def _close_rebuild_locks_at_exit() -> None:
    """Close graph-specific runtime handles before interpreter teardown."""
    with _COORDINATORS_LOCK:
        held_locks = tuple(_REBUILD_LOCK_HANDLES.values())
        _REBUILD_LOCK_HANDLES.clear()
    for held in held_locks:
        try:
            held.close()
        except OSError:
            pass


def _reset_rebuild_locks_in_forked_child() -> None:
    """Discard inherited graph lock state without unlocking its parent."""
    global _COORDINATORS, _COORDINATORS_LOCK, _LIVE_TEMPORARIES, _REBUILD_LOCK_HANDLES
    # Do not acquire a lock inherited from a multi-threaded parent: it might be
    # permanently locked in the child. The copied dictionary is private now.
    held_locks = tuple(_REBUILD_LOCK_HANDLES.values())
    _REBUILD_LOCK_HANDLES = {}
    for held in held_locks:
        held.close_after_fork()
    _COORDINATORS = {}
    _COORDINATORS_LOCK = threading.Lock()
    _LIVE_TEMPORARIES = set()
    _PENDING_WAITERS.set(None)


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_rebuild_locks_in_forked_child)

atexit.register(_close_rebuild_locks_at_exit)


def live_owned_temporary(vault_root: Path) -> Path | None:
    # Kernel lock ownership is deliberately not represented by PID metadata.
    return None


def wait_for_current(
    vault_root: Path,
    checkpoint: GraphSyncCheckpoint | None,
    *,
    availability: Callable[[], bool] | None = None,
    timeout_seconds: float = 30.0,
) -> bool:
    """Wait for a reader-valid live sidecar covering another builder's epoch."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        status_value = status(vault_root)
        readers_confirm = availability is None or availability()
        acknowledgement = acknowledged_checkpoint(vault_root)
        checkpoint_covered = checkpoint is None or (
            acknowledgement is not None and acknowledgement.covers(checkpoint)
        )
        if status_value["state"] == "current" and checkpoint_covered and readers_confirm:
            return True
        time.sleep(0.025)
    return False


def register_temporary(path: Path) -> None:
    with _COORDINATORS_LOCK:
        _LIVE_TEMPORARIES.add(path.resolve())


def unregister_temporary(path: Path) -> None:
    with _COORDINATORS_LOCK:
        _LIVE_TEMPORARIES.discard(path.resolve())
        # A rejected publication hook may have replaced the entry with a
        # symlink. Preserve the registration's lexical absolute spelling too,
        # rather than following that untrusted replacement during cleanup.
        _LIVE_TEMPORARIES.discard(path.absolute())


def live_temporary_paths() -> set[Path]:
    with _COORDINATORS_LOCK:
        return set(_LIVE_TEMPORARIES)


def readable_sidecar(path: Path) -> Path | None:
    return None if path.name.startswith(_TEMP_PREFIX) else path


# The lexical sidecar's detached-build temp, minted at lexstore.py's
# `LexicalStore.rebuild_atomic` as
# `self.path.with_name(f"{self.path.name}.rebuild-{uuid.uuid4().hex}.tmp")`
# where `self.path.name` is always `.lexical.sqlite` (`lexstore.lexical_path`,
# issue #551). Only the glob pre-filter prefix lives here — the strict match
# is `vault.is_lexical_rebuild_runtime_file_name`, the one PUBLIC matcher for
# this shape (also used by `doctor.py`'s `_check_rebuild_temp_orphans`).
# `governance/tool.py`'s `_LEXICAL_REBUILD_TEMP_RE` still independently
# encodes the same shape for its own unrelated membership-classification
# purpose — keep it, and `lexstore.py`'s mint site itself, in sync if this
# shape ever changes.
_LEXICAL_TEMP_PREFIX = ".lexical.sqlite.rebuild-"


def sweep_abandoned_temporaries(
    vault_root: Path,
    live: Path,
    *,
    live_paths: set[Path],
    state_root: Path | None = None,
) -> list[Path]:
    """Unlink abandoned rebuild temporaries sharing `live`'s parent directory.

    Two families, one pass, deliberately asymmetric on how "abandoned" is
    proven — a matching name is necessary but never sufficient on its own:

    - Graph (`.graph-rebuild-*.sqlite[-journal|-wal|-shm]`): its mint site
      (`temporary_sidecar_path`) is only ever written by a builder that holds
      `claim_rebuild_owner` for the builder's ENTIRE run, including the final
      `replace_sidecar` (`epistemic_graph.py`). This sweep must ALSO hold that
      same claim (`probe`, below) before it may touch anything, and the claim
      is exclusive — so a successful claim here already proves no builder
      currently holds it, which means any name-matching graph temp found is
      abandoned by construction. No age check is needed or applied.
      One pre-existing, unchanged-by-this-function exception:
      `epistemic_graph.py` deliberately sets `preserve_temporary = True` and
      releases BOTH the claim and the registration (in the same `finally`, see
      its `_rebuild_all_off_boundary`) when `graph_sync.replace_sidecar`
      raises `GraphSidecarReplaceUnavailable` — the complete sidecar is kept
      recoverable rather than discarded (contract R2). That temp then sits
      unclaimed and unregistered exactly like an abandoned one, so THIS sweep
      would remove it too; `epistemic_graph._reap_preserved_temporaries` is
      the dedicated bounded reaper for that class instead. "Abandoned by
      construction" describes every OTHER graph temp this sweep can see, not
      a universal.
    - Lexical (`.lexical.sqlite.rebuild-*.tmp[-wal|-shm|-journal]`):
      `lexstore.py`'s `rebuild_atomic` has no dependency on `graph_sync`
      (adding one would invert the module dependency) and so never
      registers or claims its temp. A name match alone is therefore NOT
      evidence of abandonment for this family — a live build's temp looks
      byte-for-byte identical to an abandoned one by name and can
      legitimately be large. Only an mtime older than
      `vault.REBUILD_TEMP_STALE_AGE_SECONDS` counts as abandoned here
      (mirrors doctor.py's read-only `_check_rebuild_temp_orphans`, which
      gates its own orphan diagnostic on the same threshold for the same
      reason).

    Each family is swept inside its own `try`/`except OSError`: an unlink or
    enumeration failure in one family (an unexpected directory matching the
    glob, a transient sharing violation beyond plain `PermissionError`, ...)
    must not abort the other family's pass — this is issue #551's only
    reaper for the lexical family, so a graph-side failure silently starving
    it would resume the exact leak this function exists to close.
    """
    from . import vault as vault_module

    probe = live.with_name(f"{_TEMP_PREFIX}sweep-{secrets.token_hex(12)}.sqlite")
    if not claim_rebuild_owner(vault_root, probe, state_root=state_root):
        return []
    removed: list[Path] = []
    try:
        active_paths = {path.resolve() for path in live_paths} | live_temporary_paths()
        # (glob pre-filter, strict PUBLIC matcher, requires-mtime-staleness).
        families: tuple[tuple[str, Callable[[str], bool], bool], ...] = (
            (
                f"{_TEMP_PREFIX}*.sqlite*",
                vault_module.is_graph_rebuild_runtime_file_name,
                False,
            ),
            (
                f"{_LEXICAL_TEMP_PREFIX}*.tmp*",
                vault_module.is_lexical_rebuild_runtime_file_name,
                True,
            ),
        )
        for glob_pattern, matches_name, stale_gated in families:
            try:
                for candidate in live.parent.glob(glob_pattern):
                    if not matches_name(candidate.name):
                        continue
                    base_name = candidate.name
                    for companion_suffix in ("-journal", "-wal", "-shm"):
                        if base_name.endswith(companion_suffix):
                            base_name = base_name.removesuffix(companion_suffix)
                            break
                    if candidate.with_name(base_name).resolve() in active_paths:
                        continue
                    if stale_gated:
                        try:
                            # `Path.stat()` follows symlinks by default. A
                            # broken symlink whose own name happens to match
                            # the lexical shape therefore raises OSError here
                            # and is silently left in place rather than
                            # reaped — a small, self-limited leak (one dead
                            # link, not a growing sidecar), not a safety
                            # issue: `unlink()` below only ever removes the
                            # link itself, never a target it points at.
                            age_seconds = time.time() - candidate.stat().st_mtime
                        except OSError:
                            # Vanished between glob() and stat() (or is a
                            # broken symlink, see above): another sweep or
                            # the builder itself already resolved it, or
                            # there is nothing safely stat-able to age-check.
                            continue
                        if age_seconds <= vault_module.REBUILD_TEMP_STALE_AGE_SECONDS:
                            # Fresh mtime: this family carries no ownership
                            # signal of its own, so a recent write is the
                            # only evidence that a build is still in flight.
                            continue
                    try:
                        candidate.unlink(missing_ok=True)
                    except PermissionError:
                        # Windows readers hold delete-sharing authority. A
                        # retained complete temp is recoverable state, so
                        # leave it for the next sweep after that reader
                        # closes.
                        continue
                    removed.append(candidate)
            except OSError:
                logger.warning(
                    "abandoned-temporary sweep failed for family glob %r; other "
                    "families are still swept this pass",
                    glob_pattern,
                    exc_info=True,
                )
                continue
        return removed
    finally:
        release_rebuild_owner(vault_root, probe, state_root=state_root)


@dataclass(frozen=True)
class GraphAvailability:
    available: bool
    reason: str | None = None


def availability(
    required: GraphSyncCheckpoint | None,
    acknowledged: GraphBuildOutcome | None,
) -> GraphAvailability:
    if required is not None and (acknowledged is None or not acknowledged.covers(required)):
        return GraphAvailability(False, "checkpoint_unacknowledged")
    return GraphAvailability(True)


def status(vault_root: Path) -> dict[str, int | str]:
    epoch = classify_epoch(vault_root)
    if epoch.kind == "legacy":
        return {"state": "current", "generation": 0}
    if epoch.kind == "pre_floor":
        assert epoch.checkpoint is not None
        return {"state": "recovery_required", "generation": epoch.checkpoint.generation}
    if epoch.kind == "coherent":
        assert epoch.checkpoint is not None
        current = epoch.acknowledgement == GraphBuildOutcome.covering(epoch.checkpoint)
        return {
            "state": "current" if current else "recovery_required",
            "generation": epoch.checkpoint.generation,
        }
    if epoch.kind == "recoverable":
        if epoch.checkpoint is None:
            assert epoch.floor is not None
            return {"state": "recovery_required", "generation": epoch.floor.generation}
        assert epoch.floor is not None
        return {"state": "unavailable", "generation": epoch.floor.generation}
    generation = (
        epoch.floor.generation
        if epoch.floor is not None
        else epoch.acknowledgement.generation if epoch.acknowledgement is not None else 0
    )
    return {"state": "unavailable", "generation": generation}


def committed_graph_failure(
    checkpoint: GraphSyncCheckpoint,
    *,
    code: str = "GRAPH_SYNC_STABILIZATION_EXHAUSTED",
    remediation: str = "Run reconcile to recover the derived graph.",
) -> dict[str, str]:
    return {
        "graph_sync": "failed",
        "graph_sync_code": code,
        "graph_sync_checkpoint": checkpoint.checkpoint_sha256,
        "graph_sync_remediation": remediation,
    }


def committed_graph_pending(checkpoint: GraphSyncCheckpoint) -> dict[str, str]:
    """The canonical bytes committed; the derived graph has not converged yet.

    A fourth outcome alongside absent/`completed`/`failed`, and the reason the
    bounded join above is safe to take: a fast write that let a caller infer a
    fresh derived graph would be worse than the slow one it replaces. `failed`
    would be a different lie -- nothing failed, the registered rebuild is simply
    still running and will publish behind this response.

    Derived reads degrade rather than break while this is true: the graph lane
    falls back to wikilink expansion, `graph_context` reports
    `available: false`, and relation-filtered recall raises the existing typed
    `RETRIEVAL_INDEX_WARMING` with its own retry hint. None of them block.
    """
    return {
        "graph_sync": "pending",
        "graph_sync_code": "GRAPH_SYNC_REBUILD_IN_PROGRESS",
        "graph_sync_checkpoint": checkpoint.checkpoint_sha256,
        "graph_sync_remediation": (
            "The write is durable. Derived graph relations are still rebuilding, so "
            "relation-filtered recall may report warming and graph context may report "
            f"available: false for a short time; re-read shortly, or {_RECONCILE_HINT}"
        ),
    }


def committed_graph_queued(checkpoint: GraphSyncCheckpoint) -> dict[str, str]:
    """The canonical bytes committed; the repair is queued, not running.

    The same fourth outcome as `committed_graph_pending`, reached the other way.
    There the derived graph is behind because a registered whole-vault rebuild
    has not converged; here it is behind because the incremental pass could not
    prove itself and enqueued the affected paths for a drain instead. The
    distinction is worth a separate code rather than reusing
    `GRAPH_SYNC_REBUILD_IN_PROGRESS`: nothing is rebuilding, so an operator
    reading that code would go looking for a flight that does not exist, and the
    two states converge on very different timescales.
    """
    return {
        "graph_sync": "pending",
        "graph_sync_code": "GRAPH_SYNC_REPAIR_QUEUED",
        "graph_sync_checkpoint": checkpoint.checkpoint_sha256,
        "graph_sync_remediation": (
            "The write is durable. Repair of the changed pages is queued and converges on "
            "the next index drain, so relation-filtered recall may report warming and graph "
            f"context may report available: false until then; re-read shortly, or {_RECONCILE_HINT}"
        ),
    }
