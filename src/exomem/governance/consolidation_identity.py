"""Private, authenticated cell identity for governed vault consolidation.

This module is deliberately separate from request schemas and vault content.
It composes the existing authorization-custody trust root; callers never supply
logical, installation, root-binding, signing, or registry identity material.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import hosted_portability, writer_lease
from . import authorization_custody, authorization_serving_membership
from .principal import OWNER_AUDIENCE, RequestPrincipal

IDENTITY_SCHEMA = "exomem.consolidation-cell-identity/v1"
IDENTITY_TRANSFER_SCHEMA = "vault-identity-transfer/v1"
FAILOVER_TARGET_CANDIDATE_SCHEMA = (
    "exomem.consolidation-failover-target-candidate/v1"
)
IDENTITY_RECORD_FIELDS = frozenset(
    {
        "schema",
        "cell_id",
        "vault_id",
        "installation_id",
        "installation_generation",
        "active_fence_digest",
        "root_binding_id",
        "root_binding_digest",
        "machine_key_id",
        "adoption_census_digest",
        "clone_of_vault_id",
        "clone_of_installation_id",
        "clone_of_snapshot_digest",
        "created_at",
        "authentication_algorithm",
        "record_digest",
        "authentication",
    }
)
IDENTITY_TRANSFER_RECORD_FIELDS = frozenset(
    {
        "schema",
        "transfer_id",
        "operation_id",
        "vault_id",
        "source_installation_id",
        "source_installation_generation",
        "source_active_fence_digest",
        "source_root_binding_id",
        "target_installation_id",
        "target_installation_generation",
        "target_challenge",
        "target_root_binding_id",
        "target_candidate_id",
        "target_candidate_digest",
        "source_clone_of_vault_id",
        "source_clone_of_installation_id",
        "source_clone_of_snapshot_digest",
        "archive_digest",
        "manifest_digest",
        "census_digest",
        "checkpoint_digest",
        "attachment_acknowledgement",
        "attachment_acknowledgement_digest",
        "reserved_control",
        "reserved_control_digest",
        "state",
        "issued_at",
        "expires_at",
        "machine_key_id",
        "authentication_algorithm",
        "record_digest",
        "authentication",
    }
)
FAILOVER_TARGET_CANDIDATE_RECORD_FIELDS = frozenset(
    {
        "schema",
        "candidate_id",
        "operation_id",
        "target_installation_id",
        "target_challenge",
        "target_root_binding_id",
        "target_census_digest",
        "issued_at",
        "expires_at",
        "machine_key_id",
        "authentication_algorithm",
        "record_digest",
        "authentication",
    }
)

_IDENTITY_DOMAIN = b"exomem.consolidation-cell-identity/v1"
_FENCE_DOMAIN = b"exomem.consolidation-installation-fence/v1"
_TRANSFER_DOMAIN = b"exomem.vault-identity-transfer/v1"
_TRANSFER_CHECKPOINT_DOMAIN = b"exomem.vault-identity-transfer-checkpoint/v1"
_TARGET_CANDIDATE_DOMAIN = b"exomem.consolidation-failover-target-candidate/v1"
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,511}\Z")
_INSTALLATION_ID = re.compile(r"installation-v1-[0-9a-f]{64}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_LOCAL_IDENTITY_DIRECTORY = "consolidation-cell-identities-v1"
_LOCAL_TRANSFER_DIRECTORY = "consolidation-identity-transfers-v1"
_LOCAL_TARGET_CANDIDATE_DIRECTORY = "consolidation-failover-target-candidates-v1"
_HOSTED_IDENTITY_NAME = "consolidation-cell-identity-v1.json"
_AUTH_ALGORITHM = "HMAC-SHA256"
_LOCAL_IDENTITY_FILE = re.compile(r"[0-9a-f]{64}\.json\Z")
_LOCAL_OWNER_ISSUERS = frozenset(
    {
        "cli-local-owner",
        "library-local-owner",
        "mcp-local-stdio",
        "rest-api-key",
        "transfer-local-owner",
    }
)


class ConsolidationIdentityUnavailable(RuntimeError):
    """Stable, content-free refusal to establish consolidation identity."""

    code = "CONSOLIDATION_IDENTITY_UNAVAILABLE"

    def __init__(self) -> None:
        super().__init__("consolidation identity is unavailable")


@dataclass(frozen=True, slots=True)
class ConsolidationCellIdentity:
    """Authenticated, non-secret identity facts for one active installation."""

    schema: str
    cell_id: str
    vault_id: str
    installation_id: str
    installation_generation: int
    active_fence_digest: str
    root_binding_id: str
    root_binding_digest: str
    machine_key_id: str
    adoption_census_digest: str
    clone_of_vault_id: str | None
    clone_of_installation_id: str | None
    clone_of_snapshot_digest: str | None
    created_at: int
    authentication_algorithm: str
    record_digest: str
    identity_path: Path = field(compare=False)


@dataclass(frozen=True, slots=True)
class LocalFailoverTargetCandidate:
    """Target-minted installation identity held under target host trust."""

    schema: str
    candidate_id: str
    operation_id: str
    target_installation_id: str
    target_challenge: str
    target_root_binding_id: str
    target_census_digest: str
    issued_at: int
    expires_at: int
    machine_key_id: str
    authentication_algorithm: str
    record_digest: str
    candidate_path: Path = field(compare=False)


@dataclass(frozen=True, slots=True)
class LocalIdentityTransfer:
    """Authenticated local failover transfer and its exact recovery basis."""

    schema: str
    transfer_id: str
    operation_id: str
    vault_id: str
    source_installation_id: str
    source_installation_generation: int
    source_active_fence_digest: str
    source_root_binding_id: str
    target_installation_id: str
    target_installation_generation: int
    target_challenge: str
    target_root_binding_id: str
    target_candidate_id: str
    target_candidate_digest: str
    source_clone_of_vault_id: str | None
    source_clone_of_installation_id: str | None
    source_clone_of_snapshot_digest: str | None
    archive_digest: str
    manifest_digest: str
    census_digest: str
    checkpoint_digest: str
    attachment_acknowledgement: bytes = field(repr=False)
    attachment_acknowledgement_digest: str
    reserved_control: bytes | None = field(repr=False)
    reserved_control_digest: str | None
    state: str
    issued_at: int
    expires_at: int
    machine_key_id: str
    authentication_algorithm: str
    record_digest: str
    transfer_path: Path = field(compare=False)


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise ConsolidationIdentityUnavailable from None


def _identifier(value: object) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ConsolidationIdentityUnavailable
    return value


def _digest(value: object) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ConsolidationIdentityUnavailable
    return value


def _time(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConsolidationIdentityUnavailable
    return value


def _closed_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ConsolidationIdentityUnavailable
        value[key] = item
    return value


def _without_authentication(value: dict[str, object]) -> dict[str, object]:
    return {key: item for key, item in value.items() if key != "authentication"}


def _without_commitments(value: dict[str, object]) -> dict[str, object]:
    return {
        key: item
        for key, item in value.items()
        if key not in {"record_digest", "authentication"}
    }


def _record_digest(value: dict[str, object]) -> str:
    return hashlib.sha256(_IDENTITY_DOMAIN + b"\0" + _canonical_json(value)).hexdigest()


def _authentication(value: dict[str, object], *, key: bytes) -> str:
    if not isinstance(key, bytes) or len(key) != 32:
        raise ConsolidationIdentityUnavailable
    return (
        base64.urlsafe_b64encode(
            hmac.new(
                key,
                _IDENTITY_DOMAIN + b"\0" + _canonical_json(value),
                hashlib.sha256,
            ).digest()
        )
        .rstrip(b"=")
        .decode("ascii")
    )


def _transfer_record_digest(value: dict[str, object]) -> str:
    return hashlib.sha256(_TRANSFER_DOMAIN + b"\0" + _canonical_json(value)).hexdigest()


def _transfer_authentication(value: dict[str, object], *, key: bytes) -> str:
    if not isinstance(key, bytes) or len(key) != 32:
        raise ConsolidationIdentityUnavailable
    return (
        base64.urlsafe_b64encode(
            hmac.new(
                key,
                _TRANSFER_DOMAIN + b"\0" + _canonical_json(value),
                hashlib.sha256,
            ).digest()
        )
        .rstrip(b"=")
        .decode("ascii")
    )


def _target_candidate_record_digest(value: dict[str, object]) -> str:
    return hashlib.sha256(
        _TARGET_CANDIDATE_DOMAIN + b"\0" + _canonical_json(value)
    ).hexdigest()


def _target_candidate_authentication(
    value: dict[str, object],
    *,
    key: bytes,
) -> str:
    if not isinstance(key, bytes) or len(key) != 32:
        raise ConsolidationIdentityUnavailable
    return (
        base64.urlsafe_b64encode(
            hmac.new(
                key,
                _TARGET_CANDIDATE_DOMAIN + b"\0" + _canonical_json(value),
                hashlib.sha256,
            ).digest()
        )
        .rstrip(b"=")
        .decode("ascii")
    )


def _decode_base64url(value: object) -> bytes:
    if not isinstance(value, str) or not value or "=" in value:
        raise ConsolidationIdentityUnavailable
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError):
        raise ConsolidationIdentityUnavailable from None
    if base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii") != value:
        raise ConsolidationIdentityUnavailable
    return decoded


def _derived_transfer_identifier(
    prefix: str,
    *,
    basis: bytes,
    key: bytes,
) -> str:
    if not isinstance(key, bytes) or len(key) != 32:
        raise ConsolidationIdentityUnavailable
    material = _TRANSFER_DOMAIN + b"\0" + prefix.encode("ascii") + b"\0" + basis
    digest = hmac.new(key, material, hashlib.sha256).hexdigest()
    return f"{prefix}-{digest}"


def _new_installation_id() -> str:
    return f"installation-v1-{secrets.token_hex(32)}"


def _root_binding_digest(root_binding_id: str) -> str:
    return hashlib.sha256(root_binding_id.encode("utf-8")).hexdigest()


def _active_fence_digest(
    *,
    vault_id: str,
    installation_id: str,
    generation: int,
    root_binding_digest: str,
    machine_key_id: str,
) -> str:
    return hashlib.sha256(
        _FENCE_DOMAIN
        + b"\0"
        + _canonical_json(
            {
                "generation": generation,
                "installation_id": installation_id,
                "machine_key_id": machine_key_id,
                "root_binding_digest": root_binding_digest,
                "vault_id": vault_id,
            }
        )
    ).hexdigest()


def _canonical_adoption_census(vault_root: Path) -> str:
    try:
        return hosted_portability.canonical_vault_fingerprint(Path(vault_root))
    except hosted_portability.PortabilityError:
        raise ConsolidationIdentityUnavailable from None


def _identity_value(
    *,
    cell_id: str,
    vault_id: str,
    installation_id: str,
    root_binding_id: str,
    machine_key_id: str,
    adoption_census_digest: str,
    created_at: int,
    installation_generation: int = 1,
    clone_of_vault_id: str | None = None,
    clone_of_installation_id: str | None = None,
    clone_of_snapshot_digest: str | None = None,
) -> dict[str, object]:
    clone_values = (
        clone_of_vault_id,
        clone_of_installation_id,
        clone_of_snapshot_digest,
    )
    if any(value is None for value in clone_values) and any(
        value is not None for value in clone_values
    ):
        raise ConsolidationIdentityUnavailable
    if clone_of_vault_id is not None:
        clone_of_vault_id = _identifier(clone_of_vault_id)
        clone_of_installation_id = _identifier(clone_of_installation_id)
        clone_of_snapshot_digest = _digest(clone_of_snapshot_digest)
    generation = _time(installation_generation)
    binding_digest = _root_binding_digest(root_binding_id)
    value: dict[str, object] = {
        "schema": IDENTITY_SCHEMA,
        "cell_id": _identifier(cell_id),
        "vault_id": _identifier(vault_id),
        "installation_id": _identifier(installation_id),
        "installation_generation": generation,
        "active_fence_digest": _active_fence_digest(
            vault_id=vault_id,
            installation_id=installation_id,
            generation=generation,
            root_binding_digest=binding_digest,
            machine_key_id=machine_key_id,
        ),
        "root_binding_id": _identifier(root_binding_id),
        "root_binding_digest": binding_digest,
        "machine_key_id": _identifier(machine_key_id),
        "adoption_census_digest": _digest(adoption_census_digest),
        "clone_of_vault_id": clone_of_vault_id,
        "clone_of_installation_id": clone_of_installation_id,
        "clone_of_snapshot_digest": clone_of_snapshot_digest,
        "created_at": _time(created_at),
        "authentication_algorithm": _AUTH_ALGORITHM,
    }
    value["record_digest"] = _record_digest(value)
    return value


def _encode_identity(value: dict[str, object], *, key: bytes) -> bytes:
    if set(value) != IDENTITY_RECORD_FIELDS - {"authentication"}:
        raise ConsolidationIdentityUnavailable
    encoded = dict(value)
    encoded["authentication"] = _authentication(encoded, key=key)
    return _canonical_json(encoded)


def _parse_identity(
    raw: bytes,
    *,
    key: bytes,
    expected_path: Path,
) -> ConsolidationCellIdentity:
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_closed_object)
        if not isinstance(value, dict) or set(value) != IDENTITY_RECORD_FIELDS:
            raise ConsolidationIdentityUnavailable
        if value["schema"] != IDENTITY_SCHEMA:
            raise ConsolidationIdentityUnavailable
        if value["authentication_algorithm"] != _AUTH_ALGORITHM:
            raise ConsolidationIdentityUnavailable
        installation_id = _identifier(value["installation_id"])
        if _INSTALLATION_ID.fullmatch(installation_id) is None:
            raise ConsolidationIdentityUnavailable
        generation = _time(value["installation_generation"])
        clone_values = (
            value["clone_of_vault_id"],
            value["clone_of_installation_id"],
            value["clone_of_snapshot_digest"],
        )
        if any(item is None for item in clone_values) and any(
            item is not None for item in clone_values
        ):
            raise ConsolidationIdentityUnavailable
        clone_of_vault_id = (
            None if clone_values[0] is None else _identifier(clone_values[0])
        )
        clone_of_installation_id = (
            None if clone_values[1] is None else _identifier(clone_values[1])
        )
        clone_of_snapshot_digest = (
            None if clone_values[2] is None else _digest(clone_values[2])
        )
        expected_digest = _record_digest(_without_commitments(value))
        if not hmac.compare_digest(_digest(value["record_digest"]), expected_digest):
            raise ConsolidationIdentityUnavailable
        expected_authentication = _authentication(
            _without_authentication(value),
            key=key,
        )
        authentication = value["authentication"]
        if not isinstance(authentication, str) or not hmac.compare_digest(
            authentication,
            expected_authentication,
        ):
            raise ConsolidationIdentityUnavailable
        identity = ConsolidationCellIdentity(
            schema=IDENTITY_SCHEMA,
            cell_id=_identifier(value["cell_id"]),
            vault_id=_identifier(value["vault_id"]),
            installation_id=installation_id,
            installation_generation=generation,
            active_fence_digest=_digest(value["active_fence_digest"]),
            root_binding_id=_identifier(value["root_binding_id"]),
            root_binding_digest=_digest(value["root_binding_digest"]),
            machine_key_id=_identifier(value["machine_key_id"]),
            adoption_census_digest=_digest(value["adoption_census_digest"]),
            clone_of_vault_id=clone_of_vault_id,
            clone_of_installation_id=clone_of_installation_id,
            clone_of_snapshot_digest=clone_of_snapshot_digest,
            created_at=_time(value["created_at"]),
            authentication_algorithm=_AUTH_ALGORITHM,
            record_digest=_digest(value["record_digest"]),
            identity_path=expected_path,
        )
        if not hmac.compare_digest(
            identity.root_binding_digest,
            _root_binding_digest(identity.root_binding_id),
        ) or not hmac.compare_digest(
            identity.active_fence_digest,
            _active_fence_digest(
                vault_id=identity.vault_id,
                installation_id=identity.installation_id,
                generation=identity.installation_generation,
                root_binding_digest=identity.root_binding_digest,
                machine_key_id=identity.machine_key_id,
            ),
        ):
            raise ConsolidationIdentityUnavailable
        return identity
    except ConsolidationIdentityUnavailable:
        raise
    except (AttributeError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        raise ConsolidationIdentityUnavailable from None


def _target_candidate_value(
    *,
    candidate_id: str,
    operation_id: str,
    target_installation_id: str,
    target_challenge: str,
    target_root_binding_id: str,
    target_census_digest: str,
    issued_at: int,
    expires_at: int,
    machine_key_id: str,
) -> dict[str, object]:
    issued = _time(issued_at)
    expires = _time(expires_at)
    if expires <= issued:
        raise ConsolidationIdentityUnavailable
    value: dict[str, object] = {
        "schema": FAILOVER_TARGET_CANDIDATE_SCHEMA,
        "candidate_id": _identifier(candidate_id),
        "operation_id": _identifier(operation_id),
        "target_installation_id": _identifier(target_installation_id),
        "target_challenge": _identifier(target_challenge),
        "target_root_binding_id": _identifier(target_root_binding_id),
        "target_census_digest": _digest(target_census_digest),
        "issued_at": issued,
        "expires_at": expires,
        "machine_key_id": _identifier(machine_key_id),
        "authentication_algorithm": _AUTH_ALGORITHM,
    }
    if _INSTALLATION_ID.fullmatch(str(value["target_installation_id"])) is None:
        raise ConsolidationIdentityUnavailable
    value["record_digest"] = _target_candidate_record_digest(value)
    return value


def _encode_target_candidate(value: dict[str, object], *, key: bytes) -> bytes:
    if set(value) != FAILOVER_TARGET_CANDIDATE_RECORD_FIELDS - {"authentication"}:
        raise ConsolidationIdentityUnavailable
    encoded = dict(value)
    encoded["authentication"] = _target_candidate_authentication(encoded, key=key)
    return _canonical_json(encoded)


def _parse_target_candidate(
    raw: bytes,
    *,
    key: bytes,
    expected_path: Path,
    now: int,
    allow_expired: bool = False,
) -> LocalFailoverTargetCandidate:
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_closed_object)
        if (
            not isinstance(value, dict)
            or set(value) != FAILOVER_TARGET_CANDIDATE_RECORD_FIELDS
            or value["schema"] != FAILOVER_TARGET_CANDIDATE_SCHEMA
            or value["authentication_algorithm"] != _AUTH_ALGORITHM
        ):
            raise ConsolidationIdentityUnavailable
        issued_at = _time(value["issued_at"])
        expires_at = _time(value["expires_at"])
        current_time = _time(now)
        if (
            expires_at <= issued_at
            or current_time < issued_at
            or (not allow_expired and current_time >= expires_at)
        ):
            raise ConsolidationIdentityUnavailable
        expected_digest = _target_candidate_record_digest(
            _without_commitments(value)
        )
        if not hmac.compare_digest(_digest(value["record_digest"]), expected_digest):
            raise ConsolidationIdentityUnavailable
        authentication = value["authentication"]
        expected_authentication = _target_candidate_authentication(
            _without_authentication(value),
            key=key,
        )
        if not isinstance(authentication, str) or not hmac.compare_digest(
            authentication,
            expected_authentication,
        ):
            raise ConsolidationIdentityUnavailable
        installation_id = _identifier(value["target_installation_id"])
        if _INSTALLATION_ID.fullmatch(installation_id) is None:
            raise ConsolidationIdentityUnavailable
        return LocalFailoverTargetCandidate(
            schema=FAILOVER_TARGET_CANDIDATE_SCHEMA,
            candidate_id=_identifier(value["candidate_id"]),
            operation_id=_identifier(value["operation_id"]),
            target_installation_id=installation_id,
            target_challenge=_identifier(value["target_challenge"]),
            target_root_binding_id=_identifier(value["target_root_binding_id"]),
            target_census_digest=_digest(value["target_census_digest"]),
            issued_at=issued_at,
            expires_at=expires_at,
            machine_key_id=_identifier(value["machine_key_id"]),
            authentication_algorithm=_AUTH_ALGORITHM,
            record_digest=_digest(value["record_digest"]),
            candidate_path=expected_path,
        )
    except ConsolidationIdentityUnavailable:
        raise
    except (AttributeError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        raise ConsolidationIdentityUnavailable from None


def _transfer_value(
    *,
    transfer_id: str,
    operation_id: str,
    vault_id: str,
    source_installation_id: str,
    source_installation_generation: int,
    source_active_fence_digest: str,
    source_root_binding_id: str,
    target_installation_id: str,
    target_installation_generation: int,
    target_challenge: str,
    target_root_binding_id: str,
    target_candidate_id: str,
    target_candidate_digest: str,
    source_clone_of_vault_id: str | None,
    source_clone_of_installation_id: str | None,
    source_clone_of_snapshot_digest: str | None,
    archive_digest: str,
    manifest_digest: str,
    census_digest: str,
    checkpoint_digest: str,
    attachment_acknowledgement: bytes,
    reserved_control: bytes | None,
    state: str,
    issued_at: int,
    expires_at: int,
    machine_key_id: str,
) -> dict[str, object]:
    source_generation = _time(source_installation_generation)
    target_generation = _time(target_installation_generation)
    issued = _time(issued_at)
    expires = _time(expires_at)
    clone_values = (
        source_clone_of_vault_id,
        source_clone_of_installation_id,
        source_clone_of_snapshot_digest,
    )
    if any(item is None for item in clone_values) and any(
        item is not None for item in clone_values
    ):
        raise ConsolidationIdentityUnavailable
    if source_clone_of_vault_id is not None:
        source_clone_of_vault_id = _identifier(source_clone_of_vault_id)
        source_clone_of_installation_id = _identifier(
            source_clone_of_installation_id
        )
        source_clone_of_snapshot_digest = _digest(source_clone_of_snapshot_digest)
    if (
        target_generation != source_generation + 1
        or expires <= issued
        or state not in {"source-active", "source-fenced-target-pending", "target-active"}
        or not isinstance(attachment_acknowledgement, bytes)
        or not 1 <= len(attachment_acknowledgement) <= authorization_custody.MAX_CUSTODY_FILE_BYTES
    ):
        raise ConsolidationIdentityUnavailable
    if state == "source-active":
        if reserved_control is not None:
            raise ConsolidationIdentityUnavailable
    elif (
        not isinstance(reserved_control, bytes)
        or not 1
        <= len(reserved_control)
        <= authorization_custody.MAX_CUSTODY_FILE_BYTES
    ):
        raise ConsolidationIdentityUnavailable
    value: dict[str, object] = {
        "schema": IDENTITY_TRANSFER_SCHEMA,
        "transfer_id": _identifier(transfer_id),
        "operation_id": _identifier(operation_id),
        "vault_id": _identifier(vault_id),
        "source_installation_id": _identifier(source_installation_id),
        "source_installation_generation": source_generation,
        "source_active_fence_digest": _digest(source_active_fence_digest),
        "source_root_binding_id": _identifier(source_root_binding_id),
        "target_installation_id": _identifier(target_installation_id),
        "target_installation_generation": target_generation,
        "target_challenge": _identifier(target_challenge),
        "target_root_binding_id": _identifier(target_root_binding_id),
        "target_candidate_id": _identifier(target_candidate_id),
        "target_candidate_digest": _digest(target_candidate_digest),
        "source_clone_of_vault_id": source_clone_of_vault_id,
        "source_clone_of_installation_id": source_clone_of_installation_id,
        "source_clone_of_snapshot_digest": source_clone_of_snapshot_digest,
        "archive_digest": _digest(archive_digest),
        "manifest_digest": _digest(manifest_digest),
        "census_digest": _digest(census_digest),
        "checkpoint_digest": _digest(checkpoint_digest),
        "attachment_acknowledgement": (
            base64.urlsafe_b64encode(attachment_acknowledgement)
            .rstrip(b"=")
            .decode("ascii")
        ),
        "attachment_acknowledgement_digest": hashlib.sha256(
            attachment_acknowledgement
        ).hexdigest(),
        "reserved_control": (
            None
            if reserved_control is None
            else base64.urlsafe_b64encode(reserved_control)
            .rstrip(b"=")
            .decode("ascii")
        ),
        "reserved_control_digest": (
            None
            if reserved_control is None
            else hashlib.sha256(reserved_control).hexdigest()
        ),
        "state": state,
        "issued_at": issued,
        "expires_at": expires,
        "machine_key_id": _identifier(machine_key_id),
        "authentication_algorithm": _AUTH_ALGORITHM,
    }
    value["record_digest"] = _transfer_record_digest(value)
    return value


def _encode_transfer(value: dict[str, object], *, key: bytes) -> bytes:
    if set(value) != IDENTITY_TRANSFER_RECORD_FIELDS - {"authentication"}:
        raise ConsolidationIdentityUnavailable
    encoded = dict(value)
    encoded["authentication"] = _transfer_authentication(encoded, key=key)
    return _canonical_json(encoded)


def _parse_transfer(
    raw: bytes,
    *,
    key: bytes,
    expected_path: Path,
    now: int,
    allow_expired: bool = False,
) -> LocalIdentityTransfer:
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_closed_object)
        if not isinstance(value, dict) or set(value) != IDENTITY_TRANSFER_RECORD_FIELDS:
            raise ConsolidationIdentityUnavailable
        if (
            value["schema"] != IDENTITY_TRANSFER_SCHEMA
            or value["authentication_algorithm"] != _AUTH_ALGORITHM
        ):
            raise ConsolidationIdentityUnavailable
        acknowledgement = _decode_base64url(value["attachment_acknowledgement"])
        if not hmac.compare_digest(
            _digest(value["attachment_acknowledgement_digest"]),
            hashlib.sha256(acknowledgement).hexdigest(),
        ):
            raise ConsolidationIdentityUnavailable
        source_generation = _time(value["source_installation_generation"])
        target_generation = _time(value["target_installation_generation"])
        issued_at = _time(value["issued_at"])
        expires_at = _time(value["expires_at"])
        current_time = _time(now)
        state = value["state"]
        reserved_control_value = value["reserved_control"]
        reserved_control_digest_value = value["reserved_control_digest"]
        if state == "source-active":
            if reserved_control_value is not None or reserved_control_digest_value is not None:
                raise ConsolidationIdentityUnavailable
            reserved_control = None
            reserved_control_digest = None
        else:
            reserved_control = _decode_base64url(reserved_control_value)
            reserved_control_digest = _digest(reserved_control_digest_value)
            if not hmac.compare_digest(
                hashlib.sha256(reserved_control).hexdigest(),
                reserved_control_digest,
            ):
                raise ConsolidationIdentityUnavailable
        if (
            target_generation != source_generation + 1
            or state
            not in {"source-active", "source-fenced-target-pending", "target-active"}
            or not issued_at <= current_time
            or (
                not allow_expired
                and state != "target-active"
                and current_time >= expires_at
            )
        ):
            raise ConsolidationIdentityUnavailable
        expected_digest = _transfer_record_digest(_without_commitments(value))
        if not hmac.compare_digest(_digest(value["record_digest"]), expected_digest):
            raise ConsolidationIdentityUnavailable
        expected_authentication = _transfer_authentication(
            _without_authentication(value),
            key=key,
        )
        authentication = value["authentication"]
        if not isinstance(authentication, str) or not hmac.compare_digest(
            authentication,
            expected_authentication,
        ):
            raise ConsolidationIdentityUnavailable
        source_installation = _identifier(value["source_installation_id"])
        target_installation = _identifier(value["target_installation_id"])
        if (
            _INSTALLATION_ID.fullmatch(source_installation) is None
            or _INSTALLATION_ID.fullmatch(target_installation) is None
            or hmac.compare_digest(source_installation, target_installation)
        ):
            raise ConsolidationIdentityUnavailable
        clone_values = (
            value["source_clone_of_vault_id"],
            value["source_clone_of_installation_id"],
            value["source_clone_of_snapshot_digest"],
        )
        if any(item is None for item in clone_values) and any(
            item is not None for item in clone_values
        ):
            raise ConsolidationIdentityUnavailable
        clone_of_vault_id = (
            None if clone_values[0] is None else _identifier(clone_values[0])
        )
        clone_of_installation_id = (
            None if clone_values[1] is None else _identifier(clone_values[1])
        )
        clone_of_snapshot_digest = (
            None if clone_values[2] is None else _digest(clone_values[2])
        )
        return LocalIdentityTransfer(
            schema=IDENTITY_TRANSFER_SCHEMA,
            transfer_id=_identifier(value["transfer_id"]),
            operation_id=_identifier(value["operation_id"]),
            vault_id=_identifier(value["vault_id"]),
            source_installation_id=source_installation,
            source_installation_generation=source_generation,
            source_active_fence_digest=_digest(value["source_active_fence_digest"]),
            source_root_binding_id=_identifier(value["source_root_binding_id"]),
            target_installation_id=target_installation,
            target_installation_generation=target_generation,
            target_challenge=_identifier(value["target_challenge"]),
            target_root_binding_id=_identifier(value["target_root_binding_id"]),
            target_candidate_id=_identifier(value["target_candidate_id"]),
            target_candidate_digest=_digest(value["target_candidate_digest"]),
            source_clone_of_vault_id=clone_of_vault_id,
            source_clone_of_installation_id=clone_of_installation_id,
            source_clone_of_snapshot_digest=clone_of_snapshot_digest,
            archive_digest=_digest(value["archive_digest"]),
            manifest_digest=_digest(value["manifest_digest"]),
            census_digest=_digest(value["census_digest"]),
            checkpoint_digest=_digest(value["checkpoint_digest"]),
            attachment_acknowledgement=acknowledgement,
            attachment_acknowledgement_digest=_digest(
                value["attachment_acknowledgement_digest"]
            ),
            reserved_control=reserved_control,
            reserved_control_digest=reserved_control_digest,
            state=state,
            issued_at=issued_at,
            expires_at=expires_at,
            machine_key_id=_identifier(value["machine_key_id"]),
            authentication_algorithm=_AUTH_ALGORITHM,
            record_digest=_digest(value["record_digest"]),
            transfer_path=expected_path,
        )
    except ConsolidationIdentityUnavailable:
        raise
    except (AttributeError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        raise ConsolidationIdentityUnavailable from None


def _require_local_owner(who: RequestPrincipal) -> None:
    if (
        not isinstance(who, RequestPrincipal)
        or not who.resolved
        or who.audience_id != OWNER_AUDIENCE
        or who.issuer_family not in _LOCAL_OWNER_ISSUERS
    ):
        raise ConsolidationIdentityUnavailable


def _local_identity_path(vault_id: str) -> Path:
    digest = hashlib.sha256(_identifier(vault_id).encode("utf-8")).hexdigest()
    return _local_identity_directory() / f"{digest}.json"


def _local_identity_directory() -> Path:
    return (
        authorization_custody._standalone_host_control_root()  # noqa: SLF001
        / _LOCAL_IDENTITY_DIRECTORY
    )


def _local_transfer_directory() -> Path:
    return (
        authorization_custody._standalone_host_control_root()  # noqa: SLF001
        / _LOCAL_TRANSFER_DIRECTORY
    )


def _local_target_candidate_directory() -> Path:
    return (
        authorization_custody._standalone_host_control_root()  # noqa: SLF001
        / _LOCAL_TARGET_CANDIDATE_DIRECTORY
    )


def _local_target_candidate_path(candidate_id: str) -> Path:
    digest = hashlib.sha256(_identifier(candidate_id).encode("utf-8")).hexdigest()
    return _local_target_candidate_directory() / f"{digest}.json"


def _load_local_target_candidate(
    candidate_id: str,
    *,
    host_key: bytes,
    now: int,
    allow_expired: bool = False,
) -> tuple[LocalFailoverTargetCandidate, bytes]:
    path = _local_target_candidate_path(candidate_id)
    try:
        loaded = authorization_custody._load_private_artifact(  # noqa: SLF001
            path,
            maximum_bytes=authorization_custody.MAX_CUSTODY_FILE_BYTES,
        )
        return (
            _parse_target_candidate(
                loaded.data,
                key=host_key,
                expected_path=path,
                now=now,
                allow_expired=allow_expired,
            ),
            loaded.data,
        )
    except authorization_custody.AuthorizationCustodyUnavailable:
        raise ConsolidationIdentityUnavailable from None


def _local_transfer_path(transfer_id: str) -> Path:
    digest = hashlib.sha256(_identifier(transfer_id).encode("utf-8")).hexdigest()
    return _local_transfer_directory() / f"{digest}.json"


def _load_local_transfer(
    transfer_id: str,
    *,
    host_key: bytes,
    now: int,
    allow_expired: bool = False,
) -> tuple[LocalIdentityTransfer, bytes]:
    path = _local_transfer_path(transfer_id)
    try:
        loaded = authorization_custody._load_private_artifact(  # noqa: SLF001
            path,
            maximum_bytes=authorization_custody.MAX_CUSTODY_FILE_BYTES,
        )
        return (
            _parse_transfer(
                loaded.data,
                key=host_key,
                expected_path=path,
                now=now,
                allow_expired=allow_expired,
            ),
            loaded.data,
        )
    except authorization_custody.AuthorizationCustodyUnavailable:
        raise ConsolidationIdentityUnavailable from None


def _replace_transfer_state(
    transfer: LocalIdentityTransfer,
    *,
    raw: bytes,
    state: str,
    reserved_control: bytes | None = None,
    host_key: bytes,
    now: int,
) -> tuple[LocalIdentityTransfer, bytes]:
    target = _transfer_value(
        transfer_id=transfer.transfer_id,
        operation_id=transfer.operation_id,
        vault_id=transfer.vault_id,
        source_installation_id=transfer.source_installation_id,
        source_installation_generation=transfer.source_installation_generation,
        source_active_fence_digest=transfer.source_active_fence_digest,
        source_root_binding_id=transfer.source_root_binding_id,
        target_installation_id=transfer.target_installation_id,
        target_installation_generation=transfer.target_installation_generation,
        target_challenge=transfer.target_challenge,
        target_root_binding_id=transfer.target_root_binding_id,
        target_candidate_id=transfer.target_candidate_id,
        target_candidate_digest=transfer.target_candidate_digest,
        source_clone_of_vault_id=transfer.source_clone_of_vault_id,
        source_clone_of_installation_id=transfer.source_clone_of_installation_id,
        source_clone_of_snapshot_digest=transfer.source_clone_of_snapshot_digest,
        archive_digest=transfer.archive_digest,
        manifest_digest=transfer.manifest_digest,
        census_digest=transfer.census_digest,
        checkpoint_digest=transfer.checkpoint_digest,
        attachment_acknowledgement=transfer.attachment_acknowledgement,
        reserved_control=(
            transfer.reserved_control
            if reserved_control is None and state != "source-active"
            else reserved_control
        ),
        state=state,
        issued_at=transfer.issued_at,
        expires_at=transfer.expires_at,
        machine_key_id=transfer.machine_key_id,
    )
    encoded = _encode_transfer(target, key=host_key)
    authorization_custody._replace_control_bytes(  # noqa: SLF001
        transfer.transfer_path,
        expected=raw,
        target=encoded,
    )
    return (
        _parse_transfer(
            encoded,
            key=host_key,
            expected_path=transfer.transfer_path,
            now=now,
            allow_expired=state != "source-active",
        ),
        encoded,
    )


def _assert_unique_transfer_operation(
    operation_id: str,
    *,
    target_path: Path,
    vault_id: str,
    source_installation_id: str,
    source_installation_generation: int,
    source_active_fence_digest: str,
    host_key: bytes,
    now: int,
) -> None:
    try:
        for path in sorted(
            _local_transfer_directory().iterdir(),
            key=lambda item: item.name,
        ):
            if _LOCAL_IDENTITY_FILE.fullmatch(path.name) is None:
                raise ConsolidationIdentityUnavailable
            loaded = authorization_custody._load_private_artifact(  # noqa: SLF001
                path,
                maximum_bytes=authorization_custody.MAX_CUSTODY_FILE_BYTES,
            )
            existing = _parse_transfer(
                loaded.data,
                key=host_key,
                expected_path=path,
                now=now,
                allow_expired=True,
            )
            if existing.operation_id == operation_id and path != target_path:
                raise ConsolidationIdentityUnavailable
            same_live_source_fence = (
                path != target_path
                and existing.vault_id == vault_id
                and existing.source_installation_id == source_installation_id
                and existing.source_installation_generation
                == source_installation_generation
                and existing.source_active_fence_digest
                == source_active_fence_digest
                and (
                    existing.state != "source-active"
                    or now < existing.expires_at
                )
            )
            if same_live_source_fence:
                raise ConsolidationIdentityUnavailable
    except FileNotFoundError:
        return
    except authorization_custody.AuthorizationCustodyUnavailable:
        raise ConsolidationIdentityUnavailable from None


def _assert_unique_local_claim(
    *,
    target_path: Path,
    target_value: dict[str, object],
    host_key: bytes,
) -> None:
    directory = _local_identity_directory()
    target_installation = _identifier(target_value["installation_id"])
    target_binding = _identifier(target_value["root_binding_id"])
    try:
        for path in sorted(directory.iterdir(), key=lambda item: item.name):
            if path == target_path:
                continue
            if _LOCAL_IDENTITY_FILE.fullmatch(path.name) is None:
                raise ConsolidationIdentityUnavailable
            loaded = authorization_custody._load_private_artifact(  # noqa: SLF001
                path,
                maximum_bytes=authorization_custody.MAX_CUSTODY_FILE_BYTES,
            )
            identity = _parse_identity(loaded.data, key=host_key, expected_path=path)
            if (
                hmac.compare_digest(identity.installation_id, target_installation)
                or hmac.compare_digest(identity.root_binding_id, target_binding)
            ):
                raise ConsolidationIdentityUnavailable
    except FileNotFoundError:
        return
    except authorization_custody.AuthorizationCustodyUnavailable:
        raise ConsolidationIdentityUnavailable from None


def _assert_registered_local_identity(
    identity: ConsolidationCellIdentity,
    *,
    host_key: bytes,
) -> None:
    _assert_unique_local_claim(
        target_path=identity.identity_path,
        target_value={
            "installation_id": identity.installation_id,
            "root_binding_id": identity.root_binding_id,
        },
        host_key=host_key,
    )


def _local_identity_for_root(
    vault_root: Path,
    *,
    host_key: bytes,
) -> ConsolidationCellIdentity:
    binding = authorization_custody.standalone_attachment_id(vault_root)
    matched: ConsolidationCellIdentity | None = None
    try:
        for path in sorted(
            _local_identity_directory().iterdir(),
            key=lambda item: item.name,
        ):
            if _LOCAL_IDENTITY_FILE.fullmatch(path.name) is None:
                raise ConsolidationIdentityUnavailable
            loaded = authorization_custody._load_private_artifact(  # noqa: SLF001
                path,
                maximum_bytes=authorization_custody.MAX_CUSTODY_FILE_BYTES,
            )
            identity = _parse_identity(loaded.data, key=host_key, expected_path=path)
            if hmac.compare_digest(identity.root_binding_id, binding):
                if matched is not None:
                    raise ConsolidationIdentityUnavailable
                matched = identity
    except authorization_custody.AuthorizationCustodyUnavailable:
        raise ConsolidationIdentityUnavailable from None
    if matched is None:
        raise ConsolidationIdentityUnavailable
    return matched


def _load_local_with_custody(
    vault_root: Path,
    *,
    custody: authorization_custody.AuthorizationCustody,
    now: int,
) -> ConsolidationCellIdentity:
    authorization_custody.require_current_standalone_registry(
        custody,
        now=now,
        require_serving=True,
    )
    path = _local_identity_path(custody.control.logical_vault_id)
    try:
        host_key = authorization_custody._load_host_control_key()  # noqa: SLF001
        loaded = authorization_custody._load_private_artifact(  # noqa: SLF001
            path,
            maximum_bytes=authorization_custody.MAX_CUSTODY_FILE_BYTES,
        )
        identity = _parse_identity(loaded.data, key=host_key, expected_path=path)
        current_binding = authorization_custody.standalone_attachment_id(vault_root)
        expected_key_id = authorization_custody._host_control_key_id(host_key)  # noqa: SLF001
        if (
            identity.cell_id != custody.control.cell_id
            or identity.vault_id != custody.control.logical_vault_id
            or identity.machine_key_id != expected_key_id
            or identity.root_binding_id != current_binding
            or identity.root_binding_id != custody.control.registry_attachment_id
        ):
            raise ConsolidationIdentityUnavailable
        _assert_registered_local_identity(identity, host_key=host_key)
        return identity
    except authorization_custody.AuthorizationCustodyUnavailable:
        raise ConsolidationIdentityUnavailable from None


def load_local_identity(
    vault_root: Path,
    *,
    now: int,
) -> ConsolidationCellIdentity:
    try:
        custody = authorization_custody.load_authorization_custody(
            Path(vault_root),
            now=now,
        )
        with writer_lease.get_manager().consistency_guard(
            _local_identity_directory(),
            operation="consolidation-identity-registry-read",
            holder_kind="consolidation-identity-registry",
        ):
            return _load_local_with_custody(
                Path(vault_root),
                custody=custody,
                now=now,
            )
    except authorization_custody.AuthorizationCustodyUnavailable:
        raise ConsolidationIdentityUnavailable from None


def adopt_local_identity(
    vault_root: Path,
    *,
    principal: RequestPrincipal,
    now: int,
) -> ConsolidationCellIdentity:
    """Adopt one local vault without accepting any identity or trust material."""

    _require_local_owner(principal)
    root = Path(vault_root)
    try:
        authorization_custody.provision_standalone_custody(root, now=now)
        custody = authorization_custody.load_authorization_custody(root, now=now)
        with writer_lease.get_manager().mutation_guard(
            root,
            operation="consolidation-identity-adopt",
            holder_kind="consolidation-identity-control",
            attachment_control=True,
            attachment_now=now,
        ):
            with writer_lease.get_manager().consistency_guard(
                _local_identity_directory(),
                operation="consolidation-identity-registry-write",
                holder_kind="consolidation-identity-registry",
            ):
                authorization_custody.require_current_standalone_registry(
                    custody,
                    now=now,
                    require_serving=True,
                )
                path = _local_identity_path(custody.control.logical_vault_id)
                if path.exists() or path.is_symlink():
                    return _load_local_with_custody(root, custody=custody, now=now)
                adoption_census = _canonical_adoption_census(root)
                host_key = authorization_custody._load_host_control_key()  # noqa: SLF001
                machine_key_id = authorization_custody._host_control_key_id(host_key)  # noqa: SLF001
                value = _identity_value(
                    cell_id=custody.control.cell_id,
                    vault_id=custody.control.logical_vault_id,
                    installation_id=_new_installation_id(),
                    root_binding_id=custody.control.registry_attachment_id,
                    machine_key_id=machine_key_id,
                    adoption_census_digest=adoption_census,
                    created_at=now,
                )
                authorization_custody._prepare_private_directory(path.parent)  # noqa: SLF001
                _assert_unique_local_claim(
                    target_path=path,
                    target_value=value,
                    host_key=host_key,
                )
                authorization_custody._publish_private_artifact(  # noqa: SLF001
                    path,
                    _encode_identity(value, key=host_key),
                    maximum_bytes=authorization_custody.MAX_CUSTODY_FILE_BYTES,
                )
                return _load_local_with_custody(root, custody=custody, now=now)
    except ConsolidationIdentityUnavailable:
        raise
    except authorization_custody.AuthorizationCustodyUnavailable:
        raise ConsolidationIdentityUnavailable from None
    except (OSError, RuntimeError, TypeError, ValueError):
        raise ConsolidationIdentityUnavailable from None


def rebind_local_identity(
    source_vault_root: Path,
    target_vault_root: Path,
    *,
    principal: RequestPrincipal,
    now: int,
) -> ConsolidationCellIdentity:
    """Move one already-fenced installation binding without changing its ids."""

    _require_local_owner(principal)
    source = Path(source_vault_root)
    target = Path(target_vault_root)
    if source == target:
        raise ConsolidationIdentityUnavailable
    try:
        custody = authorization_custody.load_authorization_custody(target, now=now)
        with writer_lease.get_manager().mutation_guard(
            target,
            operation="consolidation-identity-rebind",
            holder_kind="consolidation-identity-control",
            attachment_control=True,
            attachment_now=now,
        ):
            with writer_lease.get_manager().consistency_guard(
                _local_identity_directory(),
                operation="consolidation-identity-registry-rebind",
                holder_kind="consolidation-identity-registry",
            ):
                authorization_custody.require_current_standalone_registry(
                    custody,
                    now=now,
                    require_serving=True,
                )
                target_binding = authorization_custody.standalone_attachment_id(target)
                if target_binding != custody.control.registry_attachment_id:
                    raise ConsolidationIdentityUnavailable
                path = _local_identity_path(custody.control.logical_vault_id)
                host_key = authorization_custody._load_host_control_key()  # noqa: SLF001
                loaded = authorization_custody._load_private_artifact(  # noqa: SLF001
                    path,
                    maximum_bytes=authorization_custody.MAX_CUSTODY_FILE_BYTES,
                )
                current = _parse_identity(
                    loaded.data,
                    key=host_key,
                    expected_path=path,
                )
                expected_key_id = authorization_custody._host_control_key_id(  # noqa: SLF001
                    host_key
                )
                if (
                    current.cell_id != custody.control.cell_id
                    or current.vault_id != custody.control.logical_vault_id
                    or current.machine_key_id != expected_key_id
                ):
                    raise ConsolidationIdentityUnavailable
                if current.root_binding_id == target_binding:
                    return _load_local_with_custody(target, custody=custody, now=now)
                source_binding = authorization_custody.standalone_attachment_id(source)
                if (
                    current.root_binding_id != source_binding
                    or source_binding == target_binding
                ):
                    raise ConsolidationIdentityUnavailable
                value = _identity_value(
                    cell_id=current.cell_id,
                    vault_id=current.vault_id,
                    installation_id=current.installation_id,
                    installation_generation=current.installation_generation,
                    root_binding_id=target_binding,
                    machine_key_id=current.machine_key_id,
                    adoption_census_digest=current.adoption_census_digest,
                    created_at=current.created_at,
                    clone_of_vault_id=current.clone_of_vault_id,
                    clone_of_installation_id=current.clone_of_installation_id,
                    clone_of_snapshot_digest=current.clone_of_snapshot_digest,
                )
                _assert_unique_local_claim(
                    target_path=path,
                    target_value=value,
                    host_key=host_key,
                )
                authorization_custody._replace_control_bytes(  # noqa: SLF001
                    path,
                    expected=loaded.data,
                    target=_encode_identity(value, key=host_key),
                )
                return _load_local_with_custody(target, custody=custody, now=now)
    except ConsolidationIdentityUnavailable:
        raise
    except authorization_custody.AuthorizationCustodyUnavailable:
        raise ConsolidationIdentityUnavailable from None
    except (OSError, RuntimeError, TypeError, ValueError):
        raise ConsolidationIdentityUnavailable from None


def create_rehearsal_clone_identity(
    source_vault_root: Path,
    clone_vault_root: Path,
    *,
    principal: RequestPrincipal,
    now: int,
) -> ConsolidationCellIdentity:
    """Create one traceable rehearsal identity without copying source authority."""

    _require_local_owner(principal)
    source = Path(source_vault_root)
    clone = Path(clone_vault_root)
    if source == clone:
        raise ConsolidationIdentityUnavailable
    try:
        custody = authorization_custody.load_authorization_custody(clone, now=now)
        with writer_lease.get_manager().consistency_guard(
            source,
            operation="consolidation-clone-source-snapshot",
            holder_kind="consolidation-identity-control",
        ):
            with writer_lease.get_manager().mutation_guard(
                clone,
                operation="consolidation-clone-identity",
                holder_kind="consolidation-identity-control",
                attachment_control=True,
                attachment_now=now,
            ):
                with writer_lease.get_manager().consistency_guard(
                    _local_identity_directory(),
                    operation="consolidation-clone-registry-write",
                    holder_kind="consolidation-identity-registry",
                ):
                    authorization_custody.require_current_standalone_registry(
                        custody,
                        now=now,
                        require_serving=True,
                    )
                    host_key = authorization_custody._load_host_control_key()  # noqa: SLF001
                    source_identity = _local_identity_for_root(
                        source,
                        host_key=host_key,
                    )
                    if (
                        source_identity.cell_id == custody.control.cell_id
                        or source_identity.vault_id
                        == custody.control.logical_vault_id
                    ):
                        raise ConsolidationIdentityUnavailable
                    source_snapshot = _canonical_adoption_census(source)
                    clone_snapshot = _canonical_adoption_census(clone)
                    if not hmac.compare_digest(source_snapshot, clone_snapshot):
                        raise ConsolidationIdentityUnavailable
                    path = _local_identity_path(custody.control.logical_vault_id)
                    if path.exists() or path.is_symlink():
                        existing = _load_local_with_custody(
                            clone,
                            custody=custody,
                            now=now,
                        )
                        if (
                            existing.clone_of_vault_id
                            != source_identity.vault_id
                            or existing.clone_of_installation_id
                            != source_identity.installation_id
                            or existing.clone_of_snapshot_digest != source_snapshot
                        ):
                            raise ConsolidationIdentityUnavailable
                        return existing
                    machine_key_id = authorization_custody._host_control_key_id(  # noqa: SLF001
                        host_key
                    )
                    value = _identity_value(
                        cell_id=custody.control.cell_id,
                        vault_id=custody.control.logical_vault_id,
                        installation_id=_new_installation_id(),
                        root_binding_id=custody.control.registry_attachment_id,
                        machine_key_id=machine_key_id,
                        adoption_census_digest=clone_snapshot,
                        created_at=now,
                        clone_of_vault_id=source_identity.vault_id,
                        clone_of_installation_id=source_identity.installation_id,
                        clone_of_snapshot_digest=source_snapshot,
                    )
                    authorization_custody._prepare_private_directory(path.parent)  # noqa: SLF001
                    _assert_unique_local_claim(
                        target_path=path,
                        target_value=value,
                        host_key=host_key,
                    )
                    authorization_custody._publish_private_artifact(  # noqa: SLF001
                        path,
                        _encode_identity(value, key=host_key),
                        maximum_bytes=authorization_custody.MAX_CUSTODY_FILE_BYTES,
                    )
                    return _load_local_with_custody(
                        clone,
                        custody=custody,
                        now=now,
                    )
    except ConsolidationIdentityUnavailable:
        raise
    except authorization_custody.AuthorizationCustodyUnavailable:
        raise ConsolidationIdentityUnavailable from None
    except (OSError, RuntimeError, TypeError, ValueError):
        raise ConsolidationIdentityUnavailable from None


def _failover_identity_barrier(point: str) -> None:
    """Crash-injection seam between durable failover identity effects."""

    del point


def _drained_source_custody(
    source: Path,
    *,
    expected_control: authorization_custody.AuthorizationControlRecord,
    now: int,
) -> authorization_custody.AuthorizationCustody:
    custody = authorization_custody.load_authorization_custody(source, now=now)
    membership = custody.serving_membership
    if (
        custody.control != expected_control
        or membership is None
        or custody.local_replica_id is None
        or len(membership.replicas) != 1
    ):
        raise ConsolidationIdentityUnavailable
    replica = membership.replicas[0]
    if (
        replica.replica_id != custody.local_replica_id
        or replica.state != "DRAINING"
        or not replica.issuance_stopped
        or not replica.no_in_flight
        or membership.record_digest != custody.control.serving_membership_digest
    ):
        raise ConsolidationIdentityUnavailable
    return custody


def _transfer_checkpoint_digest(
    *,
    identity: ConsolidationCellIdentity,
    custody: authorization_custody.AuthorizationCustody,
    target_root_binding_id: str,
    archive_digest: str,
    manifest_digest: str,
    census_digest: str,
) -> str:
    return hashlib.sha256(
        _TRANSFER_CHECKPOINT_DOMAIN
        + b"\0"
        + _canonical_json(
            {
                "archive_digest": archive_digest,
                "census_digest": census_digest,
                "control_digest": authorization_custody.control_attestation_digest(
                    custody.control
                ),
                "manifest_digest": manifest_digest,
                "source_active_fence_digest": identity.active_fence_digest,
                "source_installation_generation": identity.installation_generation,
                "source_installation_id": identity.installation_id,
                "source_membership_digest": custody.control.serving_membership_digest,
                "source_root_binding_id": identity.root_binding_id,
                "target_root_binding_id": target_root_binding_id,
                "vault_id": identity.vault_id,
            }
        )
    ).hexdigest()


def _archive_census_digest(manifest: dict[str, Any]) -> str:
    try:
        value = {
            "artifact": "exomem-hosted-canonical-vault",
            "schema_version": 1,
            "classification_version": manifest["classification_version"],
            "files": manifest["files"],
        }
        return hashlib.sha256(
            hosted_portability._canonical_json(value)  # noqa: SLF001
        ).hexdigest()
    except (KeyError, TypeError, ValueError):
        raise ConsolidationIdentityUnavailable from None


def prepare_local_failover_target_candidate(
    target_vault_root: Path,
    *,
    operation_id: str,
    principal: RequestPrincipal,
    now: int,
) -> LocalFailoverTargetCandidate:
    """Mint target installation authority under the target host trust root."""

    _require_local_owner(principal)
    target = Path(target_vault_root)
    operation = _identifier(operation_id)
    try:
        with writer_lease.get_manager().consistency_guard(
            target,
            operation="consolidation-failover-target-candidate-snapshot",
            holder_kind="consolidation-identity-control",
        ):
            with writer_lease.get_manager().consistency_guard(
                _local_target_candidate_directory(),
                operation="consolidation-failover-target-candidate-write",
                holder_kind="consolidation-identity-registry",
            ):
                host_key = authorization_custody._load_host_control_key()  # noqa: SLF001
                machine_key_id = authorization_custody._host_control_key_id(  # noqa: SLF001
                    host_key
                )
                target_binding = authorization_custody.standalone_attachment_id(
                    target
                )
                target_census = _canonical_adoption_census(target)
                candidate_id = _derived_transfer_identifier(
                    "target-candidate-v1",
                    basis=operation.encode("utf-8"),
                    key=host_key,
                )
                path = _local_target_candidate_path(candidate_id)
                if path.exists() or path.is_symlink():
                    existing, _raw = _load_local_target_candidate(
                        candidate_id,
                        host_key=host_key,
                        now=now,
                    )
                    if (
                        existing.operation_id != operation
                        or existing.target_root_binding_id != target_binding
                        or existing.target_census_digest != target_census
                        or existing.machine_key_id != machine_key_id
                    ):
                        raise ConsolidationIdentityUnavailable
                    return existing
                value = _target_candidate_value(
                    candidate_id=candidate_id,
                    operation_id=operation,
                    target_installation_id=_new_installation_id(),
                    target_challenge=f"challenge-v1-{secrets.token_hex(32)}",
                    target_root_binding_id=target_binding,
                    target_census_digest=target_census,
                    issued_at=now,
                    expires_at=(
                        now
                        + authorization_serving_membership.MAX_ATTESTATION_TTL_SECONDS
                    ),
                    machine_key_id=machine_key_id,
                )
                authorization_custody._prepare_private_directory(path.parent)  # noqa: SLF001
                authorization_custody._publish_private_artifact(  # noqa: SLF001
                    path,
                    _encode_target_candidate(value, key=host_key),
                    maximum_bytes=authorization_custody.MAX_CUSTODY_FILE_BYTES,
                )
                prepared, _raw = _load_local_target_candidate(
                    candidate_id,
                    host_key=host_key,
                    now=now,
                )
                return prepared
    except ConsolidationIdentityUnavailable:
        raise
    except authorization_custody.AuthorizationCustodyUnavailable:
        raise ConsolidationIdentityUnavailable from None
    except (OSError, RuntimeError, TypeError, ValueError):
        raise ConsolidationIdentityUnavailable from None


def prepare_local_failover_identity_transfer(
    source_vault_root: Path,
    target_vault_root: Path,
    *,
    operation_id: str,
    target_candidate: LocalFailoverTargetCandidate,
    archive_path: Path,
    expected_control: authorization_custody.AuthorizationControlRecord,
    principal: RequestPrincipal,
    now: int,
) -> LocalIdentityTransfer:
    """Prepare one target-generated, archive-bound local failover transfer."""

    _require_local_owner(principal)
    source = Path(source_vault_root)
    target = Path(target_vault_root)
    operation = _identifier(operation_id)
    if source == target or not isinstance(
        target_candidate,
        LocalFailoverTargetCandidate,
    ):
        raise ConsolidationIdentityUnavailable
    try:
        with writer_lease.get_manager().mutation_guard(
            source,
            operation="consolidation-identity-failover-prepare",
            holder_kind="consolidation-identity-control",
            attachment_control=True,
            attachment_now=now,
        ):
            with writer_lease.get_manager().consistency_guard(
                target,
                operation="consolidation-identity-failover-target-snapshot",
                holder_kind="consolidation-identity-control",
            ):
                with writer_lease.get_manager().consistency_guard(
                    _local_identity_directory(),
                    operation="consolidation-identity-failover-registry",
                    holder_kind="consolidation-identity-registry",
                ):
                    custody = _drained_source_custody(
                        source,
                        expected_control=expected_control,
                        now=now,
                    )
                    host_key = authorization_custody._load_host_control_key()  # noqa: SLF001
                    candidate, _candidate_raw = _load_local_target_candidate(
                        target_candidate.candidate_id,
                        host_key=host_key,
                        now=now,
                    )
                    identity = _local_identity_for_root(source, host_key=host_key)
                    if (
                        identity.vault_id != custody.control.logical_vault_id
                        or identity.cell_id != custody.control.cell_id
                    ):
                        raise ConsolidationIdentityUnavailable
                    target_binding = authorization_custody.standalone_attachment_id(
                        target
                    )
                    if (
                        candidate != target_candidate
                        or candidate.operation_id != operation
                        or candidate.target_root_binding_id != target_binding
                        or hmac.compare_digest(identity.root_binding_id, target_binding)
                    ):
                        raise ConsolidationIdentityUnavailable
                    verified = hosted_portability.verify_export_archive(
                        archive_path,
                        expected_cell_id=identity.cell_id,
                        expected_vault_id=identity.vault_id,
                    )
                    source_census = _canonical_adoption_census(source)
                    target_census = _canonical_adoption_census(target)
                    archive_census = _archive_census_digest(verified.manifest)
                    if not (
                        hmac.compare_digest(source_census, target_census)
                        and hmac.compare_digest(source_census, archive_census)
                        and hmac.compare_digest(
                            source_census,
                            candidate.target_census_digest,
                        )
                    ):
                        raise ConsolidationIdentityUnavailable
                    manifest_digest = _digest(
                        verified.manifest["overall_digest"]["value"]
                    )
                    checkpoint_digest = _transfer_checkpoint_digest(
                        identity=identity,
                        custody=custody,
                        target_root_binding_id=target_binding,
                        archive_digest=verified.archive_sha256,
                        manifest_digest=manifest_digest,
                        census_digest=source_census,
                    )
                    basis = _canonical_json(
                        {
                            "archive_digest": verified.archive_sha256,
                            "checkpoint_digest": checkpoint_digest,
                            "manifest_digest": manifest_digest,
                            "operation_id": operation,
                            "source_active_fence_digest": identity.active_fence_digest,
                            "source_installation_generation": (
                                identity.installation_generation
                            ),
                            "source_installation_id": identity.installation_id,
                            "target_root_binding_id": target_binding,
                            "target_candidate_digest": candidate.record_digest,
                            "vault_id": identity.vault_id,
                        }
                    )
                    transfer_id = _derived_transfer_identifier(
                        "transfer-v1",
                        basis=basis,
                        key=host_key,
                    )
                    path = _local_transfer_path(transfer_id)
                    _assert_unique_transfer_operation(
                        operation,
                        target_path=path,
                        vault_id=identity.vault_id,
                        source_installation_id=identity.installation_id,
                        source_installation_generation=(
                            identity.installation_generation
                        ),
                        source_active_fence_digest=identity.active_fence_digest,
                        host_key=host_key,
                        now=now,
                    )
                    if path.exists() or path.is_symlink():
                        existing, _raw = _load_local_transfer(
                            transfer_id,
                            host_key=host_key,
                            now=now,
                        )
                        if (
                            existing.operation_id != operation
                            or existing.vault_id != identity.vault_id
                            or existing.source_installation_id
                            != identity.installation_id
                            or existing.source_installation_generation
                            != identity.installation_generation
                            or existing.source_active_fence_digest
                            != identity.active_fence_digest
                            or existing.target_root_binding_id != target_binding
                            or existing.target_candidate_id != candidate.candidate_id
                            or existing.target_candidate_digest
                            != candidate.record_digest
                            or existing.archive_digest != verified.archive_sha256
                            or existing.manifest_digest != manifest_digest
                            or existing.census_digest != source_census
                            or existing.checkpoint_digest != checkpoint_digest
                        ):
                            raise ConsolidationIdentityUnavailable
                        return existing
                    acknowledgement = (
                        authorization_custody.prepare_standalone_attachment_transfer(
                            source,
                            target,
                            expected_control=custody.control,
                            now=now,
                        )
                    )
                    expires_at = min(
                        custody.control.expires_at,
                        now
                        + authorization_serving_membership.MAX_ATTESTATION_TTL_SECONDS,
                    )
                    value = _transfer_value(
                        transfer_id=transfer_id,
                        operation_id=operation,
                        vault_id=identity.vault_id,
                        source_installation_id=identity.installation_id,
                        source_installation_generation=identity.installation_generation,
                        source_active_fence_digest=identity.active_fence_digest,
                        source_root_binding_id=identity.root_binding_id,
                        target_installation_id=candidate.target_installation_id,
                        target_installation_generation=(
                            identity.installation_generation + 1
                        ),
                        target_challenge=candidate.target_challenge,
                        target_root_binding_id=target_binding,
                        target_candidate_id=candidate.candidate_id,
                        target_candidate_digest=candidate.record_digest,
                        source_clone_of_vault_id=identity.clone_of_vault_id,
                        source_clone_of_installation_id=(
                            identity.clone_of_installation_id
                        ),
                        source_clone_of_snapshot_digest=(
                            identity.clone_of_snapshot_digest
                        ),
                        archive_digest=verified.archive_sha256,
                        manifest_digest=manifest_digest,
                        census_digest=source_census,
                        checkpoint_digest=checkpoint_digest,
                        attachment_acknowledgement=acknowledgement,
                        reserved_control=None,
                        state="source-active",
                        issued_at=now,
                        expires_at=expires_at,
                        machine_key_id=identity.machine_key_id,
                    )
                    authorization_custody._prepare_private_directory(path.parent)  # noqa: SLF001
                    authorization_custody._publish_private_artifact(  # noqa: SLF001
                        path,
                        _encode_transfer(value, key=host_key),
                        maximum_bytes=authorization_custody.MAX_CUSTODY_FILE_BYTES,
                    )
                    prepared, _raw = _load_local_transfer(
                        transfer_id,
                        host_key=host_key,
                        now=now,
                    )
                    return prepared
    except ConsolidationIdentityUnavailable:
        raise
    except (
        authorization_custody.AuthorizationCustodyUnavailable,
        hosted_portability.PortabilityError,
    ):
        raise ConsolidationIdentityUnavailable from None
    except (OSError, RuntimeError, TypeError, ValueError):
        raise ConsolidationIdentityUnavailable from None


def complete_local_failover_identity_transfer(
    source_vault_root: Path,
    target_vault_root: Path,
    *,
    transfer: LocalIdentityTransfer,
    principal: RequestPrincipal,
    now: int,
) -> ConsolidationCellIdentity:
    """Fence source N, install target N+1, then open target readiness."""

    _require_local_owner(principal)
    if not isinstance(transfer, LocalIdentityTransfer):
        raise ConsolidationIdentityUnavailable
    source = Path(source_vault_root)
    target = Path(target_vault_root)
    if source == target:
        raise ConsolidationIdentityUnavailable
    try:
        host_key = authorization_custody._load_host_control_key()  # noqa: SLF001
        current, raw = _load_local_transfer(
            transfer.transfer_id,
            host_key=host_key,
            now=now,
            allow_expired=True,
        )
        candidate, _candidate_raw = _load_local_target_candidate(
            current.target_candidate_id,
            host_key=host_key,
            now=now,
            allow_expired=True,
        )
        target_binding = authorization_custody.standalone_attachment_id(target)
        if (
            current.vault_id != transfer.vault_id
            or current.target_root_binding_id != target_binding
            or current.machine_key_id
            != authorization_custody._host_control_key_id(host_key)  # noqa: SLF001
            or current.census_digest != _canonical_adoption_census(target)
            or candidate.record_digest != current.target_candidate_digest
            or candidate.target_installation_id
            != current.target_installation_id
            or candidate.target_challenge != current.target_challenge
            or candidate.target_root_binding_id != target_binding
            or candidate.target_census_digest != current.census_digest
        ):
            raise ConsolidationIdentityUnavailable
        if current.state != "target-active" and (
            current.source_root_binding_id
            != authorization_custody.standalone_attachment_id(source)
            or current.census_digest != _canonical_adoption_census(source)
        ):
            raise ConsolidationIdentityUnavailable

        if current.state == "source-active":
            reserved = authorization_custody.reserve_standalone_attachment_transfer(
                target,
                acknowledgement=current.attachment_acknowledgement,
                now=now,
                recover_expired_reservation=now >= current.expires_at,
            )
            if (
                current.census_digest != _canonical_adoption_census(source)
                or current.census_digest != _canonical_adoption_census(target)
            ):
                raise ConsolidationIdentityUnavailable
            _failover_identity_barrier("after-reservation")
            reserved_external = authorization_custody.load_external_custody(target)
            if (
                authorization_custody.parse_control_record(
                    reserved_external.control,
                    keyring=reserved.keyring,
                    now=now,
                )
                != reserved.control
            ):
                raise ConsolidationIdentityUnavailable
            with writer_lease.get_manager().consistency_guard(
                _local_identity_directory(),
                operation="consolidation-identity-failover-pending",
                holder_kind="consolidation-identity-registry",
            ):
                current, raw = _replace_transfer_state(
                    current,
                    raw=raw,
                    state="source-fenced-target-pending",
                    reserved_control=reserved_external.control,
                    host_key=host_key,
                    now=now,
                )
            _failover_identity_barrier("after-pending-record")
        else:
            if current.reserved_control is None:
                raise ConsolidationIdentityUnavailable
            external = authorization_custody.load_external_custody(target)
            keyring = authorization_custody.parse_keyring(external.keyring)
            reserved_control = authorization_custody.parse_control_record(
                current.reserved_control,
                keyring=keyring,
                now=now,
            )
            reserved = authorization_custody.AuthorizationCustody(
                keyring_path=external.keyring_path,
                control_path=external.control_path,
                keyring=keyring,
                control=reserved_control,
            )

        if current.state != "target-active":
            with writer_lease.get_manager().consistency_guard(
                _local_identity_directory(),
                operation="consolidation-identity-failover-install",
                holder_kind="consolidation-identity-registry",
            ):
                identity_path = _local_identity_path(current.vault_id)
                loaded = authorization_custody._load_private_artifact(  # noqa: SLF001
                    identity_path,
                    maximum_bytes=authorization_custody.MAX_CUSTODY_FILE_BYTES,
                )
                installed = _parse_identity(
                    loaded.data,
                    key=host_key,
                    expected_path=identity_path,
                )
                if installed.installation_id == current.source_installation_id:
                    if (
                        installed.installation_generation
                        != current.source_installation_generation
                        or installed.active_fence_digest
                        != current.source_active_fence_digest
                        or installed.root_binding_id
                        != current.source_root_binding_id
                        or installed.vault_id != current.vault_id
                    ):
                        raise ConsolidationIdentityUnavailable
                    target_value = _identity_value(
                        cell_id=reserved.control.cell_id,
                        vault_id=current.vault_id,
                        installation_id=current.target_installation_id,
                        installation_generation=(
                            current.target_installation_generation
                        ),
                        root_binding_id=current.target_root_binding_id,
                        machine_key_id=current.machine_key_id,
                        adoption_census_digest=installed.adoption_census_digest,
                        created_at=now,
                        clone_of_vault_id=current.source_clone_of_vault_id,
                        clone_of_installation_id=(
                            current.source_clone_of_installation_id
                        ),
                        clone_of_snapshot_digest=(
                            current.source_clone_of_snapshot_digest
                        ),
                    )
                    _assert_unique_local_claim(
                        target_path=identity_path,
                        target_value=target_value,
                        host_key=host_key,
                    )
                    authorization_custody._replace_control_bytes(  # noqa: SLF001
                        identity_path,
                        expected=loaded.data,
                        target=_encode_identity(target_value, key=host_key),
                    )
                elif (
                    installed.installation_id != current.target_installation_id
                    or installed.installation_generation
                    != current.target_installation_generation
                    or installed.root_binding_id != current.target_root_binding_id
                    or installed.vault_id != current.vault_id
                    or installed.clone_of_vault_id
                    != current.source_clone_of_vault_id
                    or installed.clone_of_installation_id
                    != current.source_clone_of_installation_id
                    or installed.clone_of_snapshot_digest
                    != current.source_clone_of_snapshot_digest
                ):
                    raise ConsolidationIdentityUnavailable
            _failover_identity_barrier("after-target-identity")

            activated_custody = (
                authorization_custody.activate_reserved_standalone_attachment(
                    target,
                    expected_control=reserved.control,
                    now=now,
                    recover_expired_reservation=now >= current.expires_at,
                )
            )
            _failover_identity_barrier("after-activation")
            with writer_lease.get_manager().consistency_guard(
                _local_identity_directory(),
                operation="consolidation-identity-failover-active",
                holder_kind="consolidation-identity-registry",
            ):
                current, _raw = _replace_transfer_state(
                    current,
                    raw=raw,
                    state="target-active",
                    host_key=host_key,
                    now=now,
                )
        else:
            activated_custody = authorization_custody.load_authorization_custody(
                target,
                now=now,
            )
        return _load_local_with_custody(
            target,
            custody=activated_custody,
            now=now,
        )
    except ConsolidationIdentityUnavailable:
        raise
    except authorization_custody.AuthorizationCustodyUnavailable:
        raise ConsolidationIdentityUnavailable from None
    except (OSError, RuntimeError, TypeError, ValueError):
        raise ConsolidationIdentityUnavailable from None


def _hosted_root_binding(binding: Any) -> str:
    identities: list[dict[str, object]] = []
    try:
        for kind, root in binding.roots():
            info = os.lstat(root)
            if (
                not stat.S_ISDIR(info.st_mode)
                or stat.S_ISLNK(info.st_mode)
                or (os.name != "nt" and int(info.st_uid) != os.geteuid())
            ):
                raise ConsolidationIdentityUnavailable
            identities.append(
                {
                    "device": int(info.st_dev),
                    "inode": int(info.st_ino),
                    "kind": kind,
                }
            )
        return "hosted-binding-v2:" + hashlib.sha256(
            _canonical_json(
                {
                    "binding_digest": binding.binding_digest,
                    "root_identities": identities,
                }
            )
        ).hexdigest()
    except ConsolidationIdentityUnavailable:
        raise
    except (AttributeError, OSError, TypeError, ValueError):
        raise ConsolidationIdentityUnavailable from None


def _hosted_identity_path(binding: Any) -> Path:
    return Path(binding.state_root) / _HOSTED_IDENTITY_NAME


def _require_hosted_custody(
    binding: Any,
    custody: authorization_custody.AuthorizationCustody,
    *,
    now: int,
) -> authorization_custody.AuthorizationVerifierKey:
    if (
        binding.cell_id == binding.vault_id
        or custody.control.cell_id != binding.cell_id
        or custody.control.logical_vault_id != binding.vault_id
        or custody.keyring.cell_id != binding.cell_id
        or custody.keyring.logical_vault_id != binding.vault_id
        or custody.control.keyring_id != custody.keyring.keyring_id
    ):
        raise ConsolidationIdentityUnavailable
    current_time = _time(now)
    if not custody.control.issued_at <= current_time < custody.control.expires_at:
        raise ConsolidationIdentityUnavailable
    key = custody.keyring.active_key
    if not key.not_before <= current_time < key.not_after:
        raise ConsolidationIdentityUnavailable
    return key


def _hosted_record_key(
    raw: bytes,
    custody: authorization_custody.AuthorizationCustody,
) -> authorization_custody.AuthorizationVerifierKey:
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_closed_object)
        if not isinstance(value, dict) or set(value) != IDENTITY_RECORD_FIELDS:
            raise ConsolidationIdentityUnavailable
        key_id = _identifier(value["machine_key_id"])
        for key in custody.keyring.accepted_keys:
            if hmac.compare_digest(key.key_id, key_id):
                return key
        raise ConsolidationIdentityUnavailable
    except ConsolidationIdentityUnavailable:
        raise
    except (AttributeError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        raise ConsolidationIdentityUnavailable from None


def load_hosted_identity(
    binding: Any,
    *,
    custody: authorization_custody.AuthorizationCustody,
    now: int,
) -> ConsolidationCellIdentity:
    """Load one Hosted identity without silently provisioning a missing record."""

    _require_hosted_custody(binding, custody, now=now)
    path = _hosted_identity_path(binding)
    root_binding_id = _hosted_root_binding(binding)
    try:
        loaded = authorization_custody._load_private_artifact(  # noqa: SLF001
            path,
            maximum_bytes=authorization_custody.MAX_CUSTODY_FILE_BYTES,
        )
    except authorization_custody.AuthorizationCustodyUnavailable:
        raise ConsolidationIdentityUnavailable from None
    key = _hosted_record_key(loaded.data, custody)
    identity = _parse_identity(loaded.data, key=key.key, expected_path=path)
    if (
        identity.cell_id != binding.cell_id
        or identity.vault_id != binding.vault_id
        or identity.machine_key_id != key.key_id
        or identity.root_binding_id != root_binding_id
    ):
        raise ConsolidationIdentityUnavailable
    return identity


def adopt_hosted_identity(
    binding: Any,
    *,
    custody: authorization_custody.AuthorizationCustody,
    now: int,
) -> ConsolidationCellIdentity:
    """Create or load one Hosted record from trusted binding/custody inputs."""

    key = _require_hosted_custody(binding, custody, now=now)
    path = _hosted_identity_path(binding)
    root_binding_id = _hosted_root_binding(binding)
    try:
        authorization_custody._load_private_artifact(  # noqa: SLF001
            path,
            maximum_bytes=authorization_custody.MAX_CUSTODY_FILE_BYTES,
        )
    except authorization_custody.AuthorizationCustodyUnavailable:
        if path.exists() or path.is_symlink():
            raise ConsolidationIdentityUnavailable from None
    else:
        return load_hosted_identity(binding, custody=custody, now=now)

    value = _identity_value(
        cell_id=binding.cell_id,
        vault_id=binding.vault_id,
        installation_id=_new_installation_id(),
        root_binding_id=root_binding_id,
        machine_key_id=key.key_id,
        adoption_census_digest=_canonical_adoption_census(Path(binding.vault_root)),
        created_at=now,
    )
    try:
        authorization_custody._publish_private_artifact(  # noqa: SLF001
            path,
            _encode_identity(value, key=key.key),
            maximum_bytes=authorization_custody.MAX_CUSTODY_FILE_BYTES,
        )
        return load_hosted_identity(binding, custody=custody, now=now)
    except authorization_custody.AuthorizationCustodyUnavailable:
        raise ConsolidationIdentityUnavailable from None


__all__ = [
    "IDENTITY_RECORD_FIELDS",
    "IDENTITY_SCHEMA",
    "IDENTITY_TRANSFER_RECORD_FIELDS",
    "IDENTITY_TRANSFER_SCHEMA",
    "ConsolidationCellIdentity",
    "ConsolidationIdentityUnavailable",
    "LocalIdentityTransfer",
    "adopt_hosted_identity",
    "adopt_local_identity",
    "complete_local_failover_identity_transfer",
    "create_rehearsal_clone_identity",
    "load_hosted_identity",
    "load_local_identity",
    "prepare_local_failover_identity_transfer",
    "rebind_local_identity",
]
