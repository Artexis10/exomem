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
from . import authorization_custody
from .principal import OWNER_AUDIENCE, RequestPrincipal

IDENTITY_SCHEMA = "exomem.consolidation-cell-identity/v1"
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

_IDENTITY_DOMAIN = b"exomem.consolidation-cell-identity/v1"
_FENCE_DOMAIN = b"exomem.consolidation-installation-fence/v1"
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,511}\Z")
_INSTALLATION_ID = re.compile(r"installation-v1-[0-9a-f]{64}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_LOCAL_IDENTITY_DIRECTORY = "consolidation-cell-identities-v1"
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
    binding_digest = _root_binding_digest(root_binding_id)
    value: dict[str, object] = {
        "schema": IDENTITY_SCHEMA,
        "cell_id": _identifier(cell_id),
        "vault_id": _identifier(vault_id),
        "installation_id": _identifier(installation_id),
        "installation_generation": 1,
        "active_fence_digest": _active_fence_digest(
            vault_id=vault_id,
            installation_id=installation_id,
            generation=1,
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
        if generation != 1:
            raise ConsolidationIdentityUnavailable
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
                    root_binding_id=target_binding,
                    machine_key_id=current.machine_key_id,
                    adoption_census_digest=current.adoption_census_digest,
                    created_at=current.created_at,
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
    "ConsolidationCellIdentity",
    "ConsolidationIdentityUnavailable",
    "adopt_hosted_identity",
    "adopt_local_identity",
    "create_rehearsal_clone_identity",
    "load_hosted_identity",
    "load_local_identity",
    "rebind_local_identity",
]
