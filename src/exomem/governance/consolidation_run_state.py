"""Durable owner-only control records for governed consolidation runs."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath

from .. import reserved_paths
from .consolidation_intake import ConsolidationInventoryItem
from .projections import ProjectionCanonicalizationError, canonical_jcs

_DESCRIPTOR_ID = "consolidation-tree"
_OWNER = "consolidation.run"
_RUN_SCHEMA = "exomem.consolidation-run/v1"
_INVENTORY_SCHEMA = "exomem.consolidation-inventory/v1"
_INVENTORY_DIGEST_DOMAIN = b"exomem.consolidation-inventory/v1"
_RUN_MODES = frozenset({"cloned-rehearsal", "real-cutover"})
_PHASES = frozenset(
    {
        "intake-complete",
        "reconciling",
        "reconciled",
        "planning",
        "planned",
        "approved",
        "sealing",
        "sealed",
        "applying",
        "verifying",
        "transport-stopping",
        "transport-verifying",
        "transport-verified",
        "routing-opening",
        "complete",
        "recovering",
        "aborting",
        "aborted",
        "rolling-back",
        "rollback-verifying",
        "rollback-complete",
        "retirement-pending-forward-only",
        "retirement-finalize",
        "blocked",
    }
)
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_EXPORT_REF = re.compile(r"exomem-export://sha256/([0-9a-f]{64})\Z")
_SOURCE_PROOF_REF = re.compile(
    r"exomem-source-attestation://sha256/([0-9a-f]{64})\Z"
)
_OBJECT_REF = re.compile(
    r"exomem-consolidation-object://sha256/([0-9a-f]{64})\Z"
)
_TIMESTAMP = re.compile(
    r"[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])"
    r"T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]\.[0-9]{3}Z\Z"
)
_CURSOR = re.compile(
    r"exomem-consolidation-inventory://"
    r"([0-9a-f-]{36})/([0-9a-f]{64})/([0-9]{1,6})\Z"
)
_MAX_SAFE_INTEGER = (1 << 53) - 1
_MAX_RUN_BYTES = 256 * 1024
_MAX_INVENTORY_BYTES = 32 * 1024 * 1024
_MAX_INVENTORY_ITEMS = 100_000
_MAX_PAGE_SIZE = 200
_IDENTITY_FIELDS = frozenset(
    {
        "archive_sha256",
        "created_at",
        "destination_fence_digest",
        "destination_generation",
        "destination_identity_binding_digest",
        "destination_installation_id",
        "destination_snapshot_fingerprint",
        "destination_vault_id",
        "manifest_sha256",
        "run_id",
        "run_mode",
        "source_artifact_ref",
        "source_attestation_ref",
        "source_census_sha256",
        "source_fingerprint",
        "source_proof_digest",
        "start_operation_id",
    }
)


class ConsolidationRunUnavailable(RuntimeError):
    """Content-free durable run refusal with a stable machine code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class ConsolidationRunIdentity:
    run_id: str
    start_operation_id: str
    run_mode: str
    destination_vault_id: str
    destination_installation_id: str
    destination_generation: int
    destination_fence_digest: str
    destination_identity_binding_digest: str
    destination_snapshot_fingerprint: str
    source_artifact_ref: str
    source_attestation_ref: str
    archive_sha256: str
    manifest_sha256: str
    source_census_sha256: str
    source_proof_digest: str
    source_fingerprint: str
    created_at: str


@dataclass(frozen=True, slots=True)
class ConsolidationRunRecord:
    identity: ConsolidationRunIdentity
    revision: int
    phase: str
    inventory_digest: str
    inventory_count: int
    created_at: str
    updated_at: str

    @property
    def run_id(self) -> str:
        return self.identity.run_id


@dataclass(frozen=True, slots=True)
class ConsolidationInventoryPage:
    run_id: str
    inventory_digest: str
    total: int
    items: tuple[ConsolidationInventoryItem, ...]
    next_cursor: str | None


def _fail(code: str) -> None:
    raise ConsolidationRunUnavailable(code)


def _uuid4(value: object) -> str:
    if not isinstance(value, str) or value != value.lower():
        _fail("RUN_INPUT_INVALID")
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, ValueError):
        _fail("RUN_INPUT_INVALID")
    if parsed.version != 4 or str(parsed) != value:
        _fail("RUN_INPUT_INVALID")
    return value


def _bounded_text(value: object, *, maximum: int = 512) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\x00" in value
        or unicodedata.normalize("NFC", value) != value
    ):
        _fail("RUN_INPUT_INVALID")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        _fail("RUN_INPUT_INVALID")
    if size > maximum:
        _fail("RUN_INPUT_INVALID")
    return value


def _digest(value: object) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        _fail("RUN_INPUT_INVALID")
    return value


def _integer(value: object, *, minimum: int = 0) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or value > _MAX_SAFE_INTEGER
    ):
        _fail("RUN_INPUT_INVALID")
    return value


def _timestamp(value: object) -> str:
    if not isinstance(value, str) or _TIMESTAMP.fullmatch(value) is None:
        _fail("RUN_INPUT_INVALID")
    try:
        datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError:
        _fail("RUN_INPUT_INVALID")
    return value


def _validated_identity(value: object) -> ConsolidationRunIdentity:
    if not isinstance(value, ConsolidationRunIdentity):
        _fail("RUN_INPUT_INVALID")
    _uuid4(value.run_id)
    _uuid4(value.start_operation_id)
    if not isinstance(value.run_mode, str) or value.run_mode not in _RUN_MODES:
        _fail("RUN_INPUT_INVALID")
    _bounded_text(value.destination_vault_id, maximum=128)
    _bounded_text(value.destination_installation_id, maximum=128)
    _integer(value.destination_generation, minimum=1)
    _digest(value.destination_fence_digest)
    _digest(value.destination_identity_binding_digest)
    _digest(value.destination_snapshot_fingerprint)
    archive = (
        _EXPORT_REF.fullmatch(value.source_artifact_ref)
        if isinstance(value.source_artifact_ref, str)
        else None
    )
    proof = (
        _SOURCE_PROOF_REF.fullmatch(value.source_attestation_ref)
        if isinstance(value.source_attestation_ref, str)
        else None
    )
    if archive is None or proof is None:
        _fail("RUN_INPUT_INVALID")
    if archive.group(1) != value.archive_sha256:
        _fail("RUN_INPUT_INVALID")
    if proof.group(1) != value.source_proof_digest:
        _fail("RUN_INPUT_INVALID")
    for digest in (
        value.archive_sha256,
        value.manifest_sha256,
        value.source_census_sha256,
        value.source_proof_digest,
        value.source_fingerprint,
    ):
        _digest(digest)
    _timestamp(value.created_at)
    return value


def _identity_dict(identity: ConsolidationRunIdentity) -> dict[str, str | int]:
    return {
        "archive_sha256": identity.archive_sha256,
        "created_at": identity.created_at,
        "destination_fence_digest": identity.destination_fence_digest,
        "destination_generation": identity.destination_generation,
        "destination_identity_binding_digest": (
            identity.destination_identity_binding_digest
        ),
        "destination_installation_id": identity.destination_installation_id,
        "destination_snapshot_fingerprint": identity.destination_snapshot_fingerprint,
        "destination_vault_id": identity.destination_vault_id,
        "manifest_sha256": identity.manifest_sha256,
        "run_id": identity.run_id,
        "run_mode": identity.run_mode,
        "source_artifact_ref": identity.source_artifact_ref,
        "source_attestation_ref": identity.source_attestation_ref,
        "source_census_sha256": identity.source_census_sha256,
        "source_fingerprint": identity.source_fingerprint,
        "source_proof_digest": identity.source_proof_digest,
        "start_operation_id": identity.start_operation_id,
    }


def _path(value: object) -> str:
    text = _bounded_text(value, maximum=4096)
    if text.startswith(("/", "\\")) or "\\" in text:
        _fail("RUN_INPUT_INVALID")
    parsed = PurePosixPath(text)
    if parsed.as_posix() != text or any(part in {"", ".", ".."} for part in parsed.parts):
        _fail("RUN_INPUT_INVALID")
    return text


def _inventory_items(
    inventory: object,
) -> tuple[ConsolidationInventoryItem, ...]:
    if not isinstance(inventory, (list, tuple)):
        _fail("RUN_INPUT_INVALID")
    if len(inventory) > _MAX_INVENTORY_ITEMS:
        _fail("RUN_RESOURCE_LIMIT")
    normalized: list[ConsolidationInventoryItem] = []
    seen_paths: set[str] = set()
    folded_paths: set[str] = set()
    for item in inventory:
        if not isinstance(item, ConsolidationInventoryItem):
            _fail("RUN_INPUT_INVALID")
        path = _path(item.path)
        folded = path.casefold()
        if path in seen_paths or folded in folded_paths:
            _fail("RUN_INPUT_INVALID")
        seen_paths.add(path)
        folded_paths.add(folded)
        size = _integer(item.size)
        digest = _digest(item.sha256)
        if not isinstance(item.classification, str) or item.classification not in {
            "canonical",
            "portable-derived",
        }:
            _fail("RUN_INPUT_INVALID")
        match = (
            _OBJECT_REF.fullmatch(item.artifact_ref)
            if isinstance(item.artifact_ref, str)
            else None
        )
        if match is None or match.group(1) != digest:
            _fail("RUN_INPUT_INVALID")
        normalized.append(
            ConsolidationInventoryItem(
                path=path,
                size=size,
                sha256=digest,
                classification=item.classification,
                artifact_ref=item.artifact_ref,
            )
        )
    return tuple(sorted(normalized, key=lambda item: item.path))


def _item_dict(item: ConsolidationInventoryItem) -> dict[str, str | int]:
    return {
        "artifact_ref": item.artifact_ref,
        "classification": item.classification,
        "path": item.path,
        "sha256": item.sha256,
        "size": item.size,
    }


def _jcs(value: object, *, limit: int) -> bytes:
    try:
        encoded = canonical_jcs(value)
    except ProjectionCanonicalizationError:
        _fail("RUN_INPUT_INVALID")
    if len(encoded) > limit:
        _fail("RUN_RESOURCE_LIMIT")
    return encoded


def _inventory_digest(items: tuple[ConsolidationInventoryItem, ...]) -> str:
    payload = _jcs([_item_dict(item) for item in items], limit=_MAX_INVENTORY_BYTES)
    framed = (
        _INVENTORY_DIGEST_DOMAIN
        + b"\x00"
        + len(payload).to_bytes(8, "big")
        + payload
    )
    return hashlib.sha256(framed).hexdigest()


def _inventory_bytes(
    run_id: str,
    items: tuple[ConsolidationInventoryItem, ...],
    digest: str,
) -> bytes:
    return _jcs(
        {
            "inventory_digest": digest,
            "items": [_item_dict(item) for item in items],
            "run_id": run_id,
            "schema": _INVENTORY_SCHEMA,
            "total": len(items),
        },
        limit=_MAX_INVENTORY_BYTES,
    )


def _record_dict(record: ConsolidationRunRecord) -> dict[str, object]:
    return {
        "created_at": record.created_at,
        "identity": _identity_dict(record.identity),
        "inventory_count": record.inventory_count,
        "inventory_digest": record.inventory_digest,
        "phase": record.phase,
        "revision": record.revision,
        "run_id": record.run_id,
        "schema": _RUN_SCHEMA,
        "updated_at": record.updated_at,
    }


def _record_bytes(record: ConsolidationRunRecord) -> bytes:
    return _jcs(_record_dict(record), limit=_MAX_RUN_BYTES)


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            _fail("RUN_STATE_CORRUPT")
        value[key] = item
    return value


def _decode(data: bytes, *, limit: int) -> object:
    if not isinstance(data, bytes) or len(data) > limit:
        _fail("RUN_STATE_CORRUPT")
    try:
        value = json.loads(data, object_pairs_hook=_pairs)
        canonical = canonical_jcs(value)
    except (json.JSONDecodeError, ProjectionCanonicalizationError, UnicodeError):
        _fail("RUN_STATE_CORRUPT")
    if canonical != data:
        _fail("RUN_STATE_CORRUPT")
    return value


def _parse_identity(value: object) -> ConsolidationRunIdentity:
    if not isinstance(value, dict) or set(value) != _IDENTITY_FIELDS:
        _fail("RUN_STATE_CORRUPT")
    try:
        identity = ConsolidationRunIdentity(**value)
        return _validated_identity(identity)
    except (TypeError, ConsolidationRunUnavailable):
        _fail("RUN_STATE_CORRUPT")


def _parse_record(data: bytes) -> ConsolidationRunRecord:
    value = _decode(data, limit=_MAX_RUN_BYTES)
    if not isinstance(value, dict) or set(value) != {
        "created_at",
        "identity",
        "inventory_count",
        "inventory_digest",
        "phase",
        "revision",
        "run_id",
        "schema",
        "updated_at",
    }:
        _fail("RUN_STATE_CORRUPT")
    try:
        identity = _parse_identity(value["identity"])
        if value["schema"] != _RUN_SCHEMA or value["run_id"] != identity.run_id:
            _fail("RUN_STATE_CORRUPT")
        revision = _integer(value["revision"], minimum=1)
        phase = value["phase"]
        if not isinstance(phase, str) or phase not in _PHASES:
            _fail("RUN_STATE_CORRUPT")
        inventory_digest = _digest(value["inventory_digest"])
        inventory_count = _integer(value["inventory_count"])
        created_at = _timestamp(value["created_at"])
        updated_at = _timestamp(value["updated_at"])
        if created_at != identity.created_at or updated_at < created_at:
            _fail("RUN_STATE_CORRUPT")
    except ConsolidationRunUnavailable:
        _fail("RUN_STATE_CORRUPT")
    return ConsolidationRunRecord(
        identity=identity,
        revision=revision,
        phase=phase,
        inventory_digest=inventory_digest,
        inventory_count=inventory_count,
        created_at=created_at,
        updated_at=updated_at,
    )


def _parse_inventory(
    data: bytes,
    *,
    expected_run_id: str,
    expected_digest: str,
    expected_count: int,
) -> tuple[ConsolidationInventoryItem, ...]:
    value = _decode(data, limit=_MAX_INVENTORY_BYTES)
    if not isinstance(value, dict) or set(value) != {
        "inventory_digest",
        "items",
        "run_id",
        "schema",
        "total",
    }:
        _fail("RUN_STATE_CORRUPT")
    try:
        if (
            value["schema"] != _INVENTORY_SCHEMA
            or value["run_id"] != expected_run_id
            or value["inventory_digest"] != expected_digest
            or value["total"] != expected_count
        ):
            _fail("RUN_STATE_CORRUPT")
        raw_items = value["items"]
        if not isinstance(raw_items, list):
            _fail("RUN_STATE_CORRUPT")
        items = _inventory_items(
            tuple(ConsolidationInventoryItem(**item) for item in raw_items)
        )
        if (
            [_item_dict(item) for item in items] != raw_items
            or _inventory_digest(items) != expected_digest
            or len(items) != expected_count
        ):
            _fail("RUN_STATE_CORRUPT")
    except (TypeError, ConsolidationRunUnavailable):
        _fail("RUN_STATE_CORRUPT")
    return items


@contextmanager
def _authority(vault_root: Path, *, mutation: bool) -> Iterator[None]:
    try:
        with reserved_paths._subsystem_authority_scope(_OWNER):  # noqa: SLF001
            with reserved_paths._identity_coordination_scope(  # noqa: SLF001
                vault_root,
                descriptor_ids=(_DESCRIPTOR_ID,),
                identity_may_change=mutation,
            ):
                yield
    except ConsolidationRunUnavailable:
        raise
    except (OSError, RuntimeError, ValueError):
        _fail("RUN_STATE_UNAVAILABLE")


class ConsolidationRunStore:
    """Persist one immutable inventory plus revisioned reserved run control."""

    def __init__(self, vault_root: Path | str):
        self.vault_root = Path(vault_root).absolute()
        self.base = (
            self.vault_root / "Knowledge Base" / "_Consolidation" / "runs"
        )

    def _run_dir(self, run_id: str) -> Path:
        return self.base / _uuid4(run_id)

    def _run_file(self, run_id: str) -> Path:
        return self._run_dir(run_id) / "run.json"

    def _inventory_file(self, run_id: str) -> Path:
        return self._run_dir(run_id) / "inventory.json"

    def _read(self, path: Path, *, limit: int) -> bytes:
        return reserved_paths._read_owner_bytes(  # noqa: SLF001
            self.vault_root,
            path,
            _DESCRIPTOR_ID,
            limit=limit,
        )

    def _publish(
        self,
        path: Path,
        data: bytes,
        *,
        expected_sha256: str | None = None,
        require_missing: bool = False,
    ) -> None:
        reserved_paths._publish_owner_bytes(  # noqa: SLF001
            self.vault_root,
            path,
            _DESCRIPTOR_ID,
            data,
            expected_sha256=expected_sha256,
            require_missing=require_missing,
        )

    def _load_locked(
        self, run_id: str
    ) -> tuple[ConsolidationRunRecord, tuple[ConsolidationInventoryItem, ...], bytes]:
        try:
            run_bytes = self._read(self._run_file(run_id), limit=_MAX_RUN_BYTES)
        except FileNotFoundError:
            _fail("RUN_NOT_FOUND")
        record = _parse_record(run_bytes)
        if record.run_id != run_id:
            _fail("RUN_STATE_CORRUPT")
        try:
            inventory = _parse_inventory(
                self._read(
                    self._inventory_file(run_id),
                    limit=_MAX_INVENTORY_BYTES,
                ),
                expected_run_id=run_id,
                expected_digest=record.inventory_digest,
                expected_count=record.inventory_count,
            )
        except FileNotFoundError:
            _fail("RUN_STATE_CORRUPT")
        return record, inventory, run_bytes

    def create(
        self,
        identity: ConsolidationRunIdentity,
        inventory: tuple[ConsolidationInventoryItem, ...],
    ) -> ConsolidationRunRecord:
        identity = _validated_identity(identity)
        items = _inventory_items(inventory)
        inventory_digest = _inventory_digest(items)
        inventory_bytes = _inventory_bytes(identity.run_id, items, inventory_digest)
        record = ConsolidationRunRecord(
            identity=identity,
            revision=1,
            phase="intake-complete",
            inventory_digest=inventory_digest,
            inventory_count=len(items),
            created_at=identity.created_at,
            updated_at=identity.created_at,
        )
        run_bytes = _record_bytes(record)
        with _authority(self.vault_root, mutation=True):
            try:
                current, current_inventory, _raw = self._load_locked(identity.run_id)
            except ConsolidationRunUnavailable as error:
                if error.code != "RUN_NOT_FOUND":
                    raise
            else:
                if current == record and current_inventory == items:
                    return current
                _fail("RUN_ID_CONFLICT")

            try:
                existing_inventory = self._read(
                    self._inventory_file(identity.run_id),
                    limit=_MAX_INVENTORY_BYTES,
                )
            except FileNotFoundError:
                self._publish(
                    self._inventory_file(identity.run_id),
                    inventory_bytes,
                    require_missing=True,
                )
            else:
                if existing_inventory != inventory_bytes:
                    _fail("RUN_ID_CONFLICT")
            self._publish(
                self._run_file(identity.run_id),
                run_bytes,
                require_missing=True,
            )
        return record

    def load(self, run_id: str) -> ConsolidationRunRecord:
        run_id = _uuid4(run_id)
        with _authority(self.vault_root, mutation=False):
            record, _inventory, _raw = self._load_locked(run_id)
        return record

    def update_phase(
        self,
        run_id: str,
        *,
        expected_revision: int,
        phase: str,
        updated_at: str,
    ) -> ConsolidationRunRecord:
        run_id = _uuid4(run_id)
        expected_revision = _integer(expected_revision, minimum=1)
        if not isinstance(phase, str) or phase not in _PHASES:
            _fail("RUN_INPUT_INVALID")
        updated_at = _timestamp(updated_at)
        with _authority(self.vault_root, mutation=True):
            current, _inventory, raw = self._load_locked(run_id)
            if current.revision != expected_revision:
                _fail("RUN_REVISION_CONFLICT")
            if updated_at < current.updated_at:
                _fail("RUN_INPUT_INVALID")
            target = ConsolidationRunRecord(
                identity=current.identity,
                revision=current.revision + 1,
                phase=phase,
                inventory_digest=current.inventory_digest,
                inventory_count=current.inventory_count,
                created_at=current.created_at,
                updated_at=updated_at,
            )
            self._publish(
                self._run_file(run_id),
                _record_bytes(target),
                expected_sha256=hashlib.sha256(raw).hexdigest(),
            )
        return target

    def page_inventory(
        self,
        run_id: str,
        *,
        cursor: str | None,
        limit: int,
    ) -> ConsolidationInventoryPage:
        run_id = _uuid4(run_id)
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= _MAX_PAGE_SIZE
        ):
            _fail("RUN_INPUT_INVALID")
        with _authority(self.vault_root, mutation=False):
            record, inventory, _raw = self._load_locked(run_id)
        offset = 0
        if cursor is not None:
            match = _CURSOR.fullmatch(cursor) if isinstance(cursor, str) else None
            if (
                match is None
                or match.group(1) != run_id
                or match.group(2) != record.inventory_digest
            ):
                _fail("RUN_CURSOR_INVALID")
            offset = int(match.group(3))
            if offset <= 0 or offset >= len(inventory):
                _fail("RUN_CURSOR_INVALID")
        stop = min(offset + limit, len(inventory))
        next_cursor = (
            None
            if stop >= len(inventory)
            else (
                f"exomem-consolidation-inventory://{run_id}/"
                f"{record.inventory_digest}/{stop}"
            )
        )
        return ConsolidationInventoryPage(
            run_id=run_id,
            inventory_digest=record.inventory_digest,
            total=len(inventory),
            items=inventory[offset:stop],
            next_cursor=next_cursor,
        )


__all__ = [
    "ConsolidationInventoryPage",
    "ConsolidationRunIdentity",
    "ConsolidationRunRecord",
    "ConsolidationRunStore",
    "ConsolidationRunUnavailable",
]
