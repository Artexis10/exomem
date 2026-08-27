"""Protected external custody inputs for authorization-session authority.

The files are deliberately loaded before their record schemas are interpreted:
path selection and filesystem custody are one trust boundary, while key/control
record authentication is the next.  Callers cannot supply either path.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

from exomem import __version__, held_fs, mutation_lock

from . import authorization_serving_membership
from .authorization_serving_membership import ServingMembershipEpoch

if TYPE_CHECKING:
    from .schema_v4 import (
        ActivationRegistryAcknowledgement,
        VerifiedActiveGovernanceState,
    )

KEYRING_FILE_ENV = "EXOMEM_AUTH_SESSION_KEYRING_FILE"
CONTROL_FILE_ENV = "EXOMEM_AUTH_SESSION_CONTROL_FILE"
MEMBERSHIP_FILE_ENV = "EXOMEM_AUTH_SESSION_MEMBERSHIP_FILE"
REPLICA_ID_ENV = "EXOMEM_AUTH_SESSION_REPLICA_ID"
MAX_CUSTODY_FILE_BYTES = 64 * 1024
_MAX_SIGNED_SQLITE_INTEGER = (1 << 63) - 1
_MAX_ACCEPTED_KEYS = 32
_KEYRING_FIELDS = frozenset(
    {
        "version",
        "keyring_id",
        "cell_id",
        "logical_vault_id",
        "active_key_id",
        "accepted_keys",
    }
)
_KEY_FIELDS = frozenset({"key_id", "key", "not_before", "not_after"})
_CONTROL_FIELDS = frozenset(
    {
        "version",
        "keyring_id",
        "cell_id",
        "logical_vault_id",
        "registry_attachment_id",
        "attachment_epoch",
        "governance_enrolled",
        "activation_store_id",
        "activation_epoch",
        "activation_state_digest",
        "serving_membership_epoch",
        "serving_membership_digest",
        "issued_at",
        "expires_at",
        "signing_key_id",
        "mac",
    }
)
_CONTROL_MAC_DOMAIN = b"exomem.authorization-session.control/v1"
_ATTACHMENT_DOMAIN = b"exomem.authorization-session.attachment/v1"
_STANDALONE_STAGING_DOMAIN = b"exomem.authorization-session.standalone-staging/v1"
_SHA256_HEX = re.compile(r"[0-9a-f]{64}\Z")
_STANDALONE_ATTACHMENT = re.compile(r"attachment-v1-[0-9a-f]{64}\Z")
_DEFAULT_KEY_TTL_SECONDS = 366 * 24 * 60 * 60
_GOVERNANCE_AUTHORITY_NAMES = (
    ".governance.sqlite",
    ".governance.sqlite-wal",
    ".governance.sqlite-shm",
    ".governance.sqlite-journal",
)


class AuthorizationCustodyUnavailable(RuntimeError):
    """Content-free refusal for any unavailable or unsafe custody input."""

    code = "AUTHORIZATION_SESSION_UNAVAILABLE"

    def __init__(self) -> None:
        super().__init__("authorization session custody is unavailable")


@dataclass(frozen=True, slots=True)
class ExternalAuthorizationCustody:
    keyring_path: Path
    control_path: Path
    keyring: bytes = field(repr=False)
    control: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class AuthorizationVerifierKey:
    key_id: str
    key: bytes = field(repr=False)
    not_before: int
    not_after: int


@dataclass(frozen=True, slots=True)
class AuthorizationKeyring:
    version: int
    keyring_id: str
    cell_id: str
    logical_vault_id: str
    active_key_id: str
    accepted_keys: tuple[AuthorizationVerifierKey, ...]

    @property
    def active_key(self) -> AuthorizationVerifierKey:
        for key in self.accepted_keys:
            if key.key_id == self.active_key_id:
                return key
        raise AuthorizationCustodyUnavailable


@dataclass(frozen=True, slots=True)
class AuthorizationControlRecord:
    version: int
    keyring_id: str
    cell_id: str
    logical_vault_id: str
    registry_attachment_id: str
    attachment_epoch: int
    governance_enrolled: bool
    activation_store_id: str | None
    activation_epoch: int | None
    activation_state_digest: str | None
    serving_membership_epoch: int
    serving_membership_digest: str
    issued_at: int
    expires_at: int
    signing_key_id: str


@dataclass(frozen=True, slots=True)
class AuthorizationCustody:
    keyring_path: Path
    control_path: Path
    keyring: AuthorizationKeyring
    control: AuthorizationControlRecord
    serving_membership: ServingMembershipEpoch | None = None
    local_replica_id: str | None = None
    membership_path: Path | None = None


@dataclass(frozen=True, slots=True)
class StandaloneProvisioningResult:
    """Non-secret identity returned by explicit standalone provisioning."""

    keyring_path: Path
    control_path: Path
    membership_path: Path
    keyring_id: str
    cell_id: str
    logical_vault_id: str
    registry_attachment_id: str
    attachment_epoch: int
    replica_id: str


@dataclass(frozen=True, slots=True)
class StandaloneV3StagingResult:
    """Non-secret identity staged for one explicit offline v3 migration."""

    keyring_path: Path
    keyring_id: str
    cell_id: str
    logical_vault_id: str
    registry_attachment_id: str
    attachment_epoch: int


@dataclass(frozen=True, slots=True)
class _LoadedCustodyFile:
    path: Path
    data: bytes = field(repr=False)
    identity: tuple[int, ...]


def _stat_identity(info: os.stat_result) -> tuple[int, ...]:
    return (
        int(info.st_dev),
        int(info.st_ino),
        int(info.st_mode),
        int(info.st_nlink),
        int(info.st_size),
        int(info.st_mtime_ns),
        int(info.st_ctime_ns),
    )


def _path_is_within(candidate: Path, root: Path) -> bool:
    try:
        common = os.path.commonpath(
            (os.path.normcase(str(candidate)), os.path.normcase(str(root)))
        )
    except ValueError:
        return False
    return common == os.path.normcase(str(root))


def _owner_identity(
    root: Path,
    info: os.stat_result,
    filesystem: held_fs.HeldFilesystem,
) -> str:
    if os.name == "nt":  # pragma: no cover - exercised by Windows CI
        import msvcrt

        descriptor = getattr(filesystem, "descriptor", None)
        if not isinstance(descriptor, int):
            raise AuthorizationCustodyUnavailable
        sid = mutation_lock._windows_current_user_sid()
        retained_handle = msvcrt.get_osfhandle(descriptor)
        security_handle = mutation_lock._windows_open_path(root, directory=True)
        try:
            if mutation_lock._windows_handle_identity(
                security_handle
            ) != mutation_lock._windows_handle_identity(retained_handle):
                raise AuthorizationCustodyUnavailable
            sddl = mutation_lock._windows_dacl_sddl_for_handle(security_handle)
        finally:
            mutation_lock._windows_close_handle(security_handle)
        if not mutation_lock._windows_owner_admits_current_user(
            mutation_lock._windows_sddl_owner(sddl),
            sid,
        ):
            raise AuthorizationCustodyUnavailable
        return f"sid:{sid}"
    owner = int(info.st_uid)
    if owner != os.geteuid():
        raise AuthorizationCustodyUnavailable
    return f"uid:{owner}"


def standalone_attachment_id(vault_root: Path) -> str:
    """Bind one standalone registration to the held vault-root identity.

    A copied vault and copied custody files therefore do not become a second
    serving attachment merely because their logical ids still match.
    """

    root = Path(vault_root)
    acquired: held_fs.HeldResult[held_fs.HeldFilesystem] | None = None
    try:
        before = os.lstat(root)
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if (
            not stat.S_ISDIR(before.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or getattr(before, "st_file_attributes", 0) & reparse
        ):
            raise AuthorizationCustodyUnavailable
        acquired = held_fs.acquire(root)
        if not acquired.ok:
            raise AuthorizationCustodyUnavailable
        with acquired.require() as filesystem:
            identity = filesystem.root_identity
            after = os.lstat(root)
            if (
                not stat.S_ISDIR(after.st_mode)
                or stat.S_ISLNK(after.st_mode)
                or getattr(after, "st_file_attributes", 0) & reparse
                or identity.kind != "directory"
                or (
                    os.name != "nt"
                    and (
                        int(after.st_dev) != identity.device
                        or int(after.st_ino) != identity.inode
                    )
                )
            ):
                raise AuthorizationCustodyUnavailable
            canonical = os.path.normcase(str(root.resolve(strict=True))).encode("utf-8")
            owner = _owner_identity(root, after, filesystem).encode("utf-8")
            digest = hashlib.sha256(
                _framed(
                    _ATTACHMENT_DOMAIN,
                    (
                        canonical,
                        str(identity.device).encode("ascii"),
                        str(identity.inode).encode("ascii"),
                        owner,
                    ),
                )
            ).hexdigest()
            return f"attachment-v1-{digest}"
    except AuthorizationCustodyUnavailable:
        raise
    except (OSError, RuntimeError, UnicodeError, ValueError, held_fs.HeldFsError):
        raise AuthorizationCustodyUnavailable from None


def _verify_registered_attachment(vault_root: Path, attachment_id: str) -> None:
    """Verify records minted by the standalone root-identity protocol.

    Existing Hosted/control-plane records use their own opaque attachment
    identifiers.  They remain the control plane's responsibility until that
    protocol is integrated; the locally minted namespace is closed and always
    verified here.
    """

    if attachment_id.startswith("attachment-v1-") and (
        _STANDALONE_ATTACHMENT.fullmatch(attachment_id) is None
        or attachment_id != standalone_attachment_id(vault_root)
    ):
        raise AuthorizationCustodyUnavailable


def _configured_external_path(variable: str, vault_root: Path) -> Path:
    raw = os.environ.get(variable)
    if raw is None or not raw or "\x00" in raw:
        raise AuthorizationCustodyUnavailable
    configured = Path(raw)
    if not configured.is_absolute():
        raise AuthorizationCustodyUnavailable
    candidate = Path(os.path.abspath(configured))
    vault = Path(os.path.abspath(vault_root))
    if _path_is_within(candidate, vault):
        raise AuthorizationCustodyUnavailable
    return candidate


def _windows_file_dacl_is_private(sddl: str, sid: str) -> bool:
    if not mutation_lock._windows_private_dacl_is_valid(
        sddl, sid, directory=False
    ):
        return False
    return "D:" in sddl and sddl.split("D:", 1)[1].startswith("P")


def _windows_file_is_private(descriptor: int) -> bool:
    import msvcrt

    handle = msvcrt.get_osfhandle(descriptor)
    sddl = mutation_lock._windows_dacl_sddl_for_handle(handle)
    sid = mutation_lock._windows_current_user_sid()
    return _windows_file_dacl_is_private(
        sddl, sid
    )


def _file_is_owner_protected(descriptor: int, info: os.stat_result) -> bool:
    if os.name == "nt":
        return _windows_file_is_private(descriptor)
    return (
        info.st_uid == os.geteuid()
        and stat.S_IMODE(info.st_mode) in {0o400, 0o600}
    )


def _read_retained(descriptor: int, expected_size: int) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    remaining = MAX_CUSTODY_FILE_BYTES + 1
    while remaining:
        chunk = os.read(descriptor, min(remaining, 4096))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    data = b"".join(chunks)
    if len(data) != expected_size or len(data) > MAX_CUSTODY_FILE_BYTES:
        raise AuthorizationCustodyUnavailable
    return data


def _load_file(path: Path) -> _LoadedCustodyFile:
    held: mutation_lock.RetainedRegularFile | None = None
    try:
        held = mutation_lock.retain_regular_file(path)
        before = os.fstat(held.fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or not 1 <= before.st_size <= MAX_CUSTODY_FILE_BYTES
            or not _file_is_owner_protected(held.fd, before)
        ):
            raise AuthorizationCustodyUnavailable
        data = _read_retained(held.fd, before.st_size)
        after = os.fstat(held.fd)
        if (
            _stat_identity(before) != _stat_identity(after)
            or not mutation_lock._same_file_entry(
                held.directory, held.path.name, held.fd
            )
        ):
            raise AuthorizationCustodyUnavailable
        return _LoadedCustodyFile(path, data, _stat_identity(after))
    except AuthorizationCustodyUnavailable:
        raise
    except (OSError, ValueError, TypeError):
        raise AuthorizationCustodyUnavailable from None
    finally:
        if held is not None:
            held.close()


def load_external_custody(vault_root: Path) -> ExternalAuthorizationCustody:
    """Load both fixed env-selected custody files through retained handles."""

    keyring_path = _configured_external_path(KEYRING_FILE_ENV, Path(vault_root))
    control_path = _configured_external_path(CONTROL_FILE_ENV, Path(vault_root))
    keyring = _load_file(keyring_path)
    control = _load_file(control_path)
    if keyring.identity[:2] == control.identity[:2]:
        raise AuthorizationCustodyUnavailable
    return ExternalAuthorizationCustody(
        keyring_path=keyring.path,
        control_path=control.path,
        keyring=keyring.data,
        control=control.data,
    )


def _closed_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for name, value in pairs:
        if name in result:
            raise AuthorizationCustodyUnavailable
        result[name] = value
    return result


def _bounded_identifier(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(ord(character) < 0x20 for character in value)
    ):
        raise AuthorizationCustodyUnavailable
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise AuthorizationCustodyUnavailable from None
    if len(encoded) > 512:
        raise AuthorizationCustodyUnavailable
    return value


def _bounded_time(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= _MAX_SIGNED_SQLITE_INTEGER
    ):
        raise AuthorizationCustodyUnavailable
    return value


def _decode_key(value: object) -> bytes:
    if not isinstance(value, str) or len(value) != 43:
        raise AuthorizationCustodyUnavailable
    try:
        encoded = value.encode("ascii")
        decoded = base64.b64decode(encoded + b"=", altchars=b"-_", validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError):
        raise AuthorizationCustodyUnavailable from None
    if (
        len(decoded) != 32
        or base64.urlsafe_b64encode(decoded).rstrip(b"=") != encoded
    ):
        raise AuthorizationCustodyUnavailable
    return decoded


def _sha256_hex(value: object) -> str:
    if not isinstance(value, str) or _SHA256_HEX.fullmatch(value) is None:
        raise AuthorizationCustodyUnavailable
    return value


def _framed(domain: bytes, fields: tuple[bytes, ...]) -> bytes:
    output = bytearray(domain)
    output.append(0)
    for value in fields:
        output.extend(len(value).to_bytes(4, "big"))
        output.extend(value)
    return bytes(output)


def runtime_software_version() -> str:
    """Return the release identity bound by the local replica attestation."""

    return __version__


def keyring_attestation_digest(keyring: AuthorizationKeyring) -> str:
    """Digest the canonical semantic keyring bound by readiness attestations."""

    if not isinstance(keyring, AuthorizationKeyring):
        raise AuthorizationCustodyUnavailable
    return hashlib.sha256(_keyring_bytes(keyring)).hexdigest()


def control_attestation_digest(record: AuthorizationControlRecord) -> str:
    """Digest the stable control identity without creating a membership cycle.

    The signed control record binds the complete membership-record digest.  A
    replica attestation therefore binds the immutable identity and attachment
    basis.  Mutable enrollment/activation/lifetime fields are independently
    authenticated and checked on every request; including them here would make
    an unrelated policy publication rewrite an otherwise unchanged fleet epoch.
    """

    if not isinstance(record, AuthorizationControlRecord):
        raise AuthorizationCustodyUnavailable
    return hashlib.sha256(
        _framed(
            b"exomem.authorization-session.control-attestation-basis/v1",
            (
                str(record.version).encode("ascii"),
                record.keyring_id.encode("utf-8"),
                record.cell_id.encode("utf-8"),
                record.logical_vault_id.encode("utf-8"),
                record.registry_attachment_id.encode("utf-8"),
                str(record.attachment_epoch).encode("ascii"),
            ),
        )
    ).hexdigest()


def _control_mac_input(value: dict[str, object]) -> bytes:
    activation_store = value["activation_store_id"]
    activation_epoch = value["activation_epoch"]
    activation_digest = value["activation_state_digest"]
    return _framed(
        _CONTROL_MAC_DOMAIN,
        (
            str(value["version"]).encode("ascii"),
            str(value["keyring_id"]).encode("utf-8"),
            str(value["cell_id"]).encode("utf-8"),
            str(value["logical_vault_id"]).encode("utf-8"),
            str(value["registry_attachment_id"]).encode("utf-8"),
            str(value["attachment_epoch"]).encode("ascii"),
            b"true" if value["governance_enrolled"] is True else b"false",
            b"" if activation_store is None else str(activation_store).encode("utf-8"),
            b"" if activation_epoch is None else str(activation_epoch).encode("ascii"),
            b"" if activation_digest is None else str(activation_digest).encode("ascii"),
            str(value["serving_membership_epoch"]).encode("ascii"),
            str(value["serving_membership_digest"]).encode("ascii"),
            str(value["issued_at"]).encode("ascii"),
            str(value["expires_at"]).encode("ascii"),
            str(value["signing_key_id"]).encode("utf-8"),
        ),
    )


def _parse_key(value: object) -> AuthorizationVerifierKey:
    if not isinstance(value, dict) or set(value) != _KEY_FIELDS:
        raise AuthorizationCustodyUnavailable
    key_id = _bounded_identifier(value["key_id"])
    not_before = _bounded_time(value["not_before"])
    not_after = _bounded_time(value["not_after"])
    if not_before >= not_after:
        raise AuthorizationCustodyUnavailable
    return AuthorizationVerifierKey(
        key_id=key_id,
        key=_decode_key(value["key"]),
        not_before=not_before,
        not_after=not_after,
    )


def parse_keyring(raw: bytes) -> AuthorizationKeyring:
    """Parse the exact version-1 verifier keyring without a permissive fallback."""

    if not isinstance(raw, bytes) or not 1 <= len(raw) <= MAX_CUSTODY_FILE_BYTES:
        raise AuthorizationCustodyUnavailable
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_closed_object)
    except AuthorizationCustodyUnavailable:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
        raise AuthorizationCustodyUnavailable from None
    if not isinstance(value, dict) or set(value) != _KEYRING_FIELDS:
        raise AuthorizationCustodyUnavailable
    version = value["version"]
    if isinstance(version, bool) or version != 1:
        raise AuthorizationCustodyUnavailable
    entries = value["accepted_keys"]
    if (
        not isinstance(entries, list)
        or not 1 <= len(entries) <= _MAX_ACCEPTED_KEYS
    ):
        raise AuthorizationCustodyUnavailable
    accepted = tuple(_parse_key(entry) for entry in entries)
    key_ids = tuple(key.key_id for key in accepted)
    if len(set(key_ids)) != len(key_ids):
        raise AuthorizationCustodyUnavailable
    active_key_id = _bounded_identifier(value["active_key_id"])
    if active_key_id not in key_ids:
        raise AuthorizationCustodyUnavailable
    return AuthorizationKeyring(
        version=1,
        keyring_id=_bounded_identifier(value["keyring_id"]),
        cell_id=_bounded_identifier(value["cell_id"]),
        logical_vault_id=_bounded_identifier(value["logical_vault_id"]),
        active_key_id=active_key_id,
        accepted_keys=accepted,
    )


def parse_control_record(
    raw: bytes,
    *,
    keyring: AuthorizationKeyring,
    now: int,
) -> AuthorizationControlRecord:
    """Authenticate the exact external control-plane record for one cell."""

    if (
        not isinstance(raw, bytes)
        or not isinstance(keyring, AuthorizationKeyring)
        or not 1 <= len(raw) <= MAX_CUSTODY_FILE_BYTES
    ):
        raise AuthorizationCustodyUnavailable
    current_time = _bounded_time(now)
    active_key = keyring.active_key
    if not active_key.not_before <= current_time < active_key.not_after:
        raise AuthorizationCustodyUnavailable
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_closed_object)
    except AuthorizationCustodyUnavailable:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
        raise AuthorizationCustodyUnavailable from None
    if not isinstance(value, dict) or set(value) != _CONTROL_FIELDS:
        raise AuthorizationCustodyUnavailable
    version = value["version"]
    if isinstance(version, bool) or version != 1:
        raise AuthorizationCustodyUnavailable

    keyring_id = _bounded_identifier(value["keyring_id"])
    cell_id = _bounded_identifier(value["cell_id"])
    logical_vault_id = _bounded_identifier(value["logical_vault_id"])
    if (
        keyring_id != keyring.keyring_id
        or cell_id != keyring.cell_id
        or logical_vault_id != keyring.logical_vault_id
    ):
        raise AuthorizationCustodyUnavailable

    registry_attachment_id = _bounded_identifier(value["registry_attachment_id"])
    attachment_epoch = _bounded_time(value["attachment_epoch"])
    serving_membership_epoch = _bounded_time(value["serving_membership_epoch"])
    serving_membership_digest = _sha256_hex(value["serving_membership_digest"])
    issued_at = _bounded_time(value["issued_at"])
    expires_at = _bounded_time(value["expires_at"])
    if not issued_at <= current_time < expires_at:
        raise AuthorizationCustodyUnavailable

    enrolled = value["governance_enrolled"]
    if not isinstance(enrolled, bool):
        raise AuthorizationCustodyUnavailable
    if enrolled:
        activation_store_id = _bounded_identifier(value["activation_store_id"])
        activation_epoch = _bounded_time(value["activation_epoch"])
        activation_state_digest = _sha256_hex(value["activation_state_digest"])
    else:
        if any(
            value[name] is not None
            for name in (
                "activation_store_id",
                "activation_epoch",
                "activation_state_digest",
            )
        ):
            raise AuthorizationCustodyUnavailable
        activation_store_id = None
        activation_epoch = None
        activation_state_digest = None

    signing_key_id = _bounded_identifier(value["signing_key_id"])
    signing_key = next(
        (key for key in keyring.accepted_keys if key.key_id == signing_key_id),
        None,
    )
    if (
        signing_key is None
        or issued_at < signing_key.not_before
        or expires_at > signing_key.not_after
    ):
        raise AuthorizationCustodyUnavailable
    supplied_mac = _decode_key(value["mac"])
    expected_mac = hmac.new(
        signing_key.key,
        _control_mac_input(value),
        hashlib.sha256,
    ).digest()
    if not hmac.compare_digest(supplied_mac, expected_mac):
        raise AuthorizationCustodyUnavailable

    return AuthorizationControlRecord(
        version=1,
        keyring_id=keyring_id,
        cell_id=cell_id,
        logical_vault_id=logical_vault_id,
        registry_attachment_id=registry_attachment_id,
        attachment_epoch=attachment_epoch,
        governance_enrolled=enrolled,
        activation_store_id=activation_store_id,
        activation_epoch=activation_epoch,
        activation_state_digest=activation_state_digest,
        serving_membership_epoch=serving_membership_epoch,
        serving_membership_digest=serving_membership_digest,
        issued_at=issued_at,
        expires_at=expires_at,
        signing_key_id=signing_key_id,
    )


def load_authorization_custody(
    vault_root: Path,
    *,
    now: int,
) -> AuthorizationCustody:
    """Load and authenticate the complete external session-custody bundle."""

    external = load_external_custody(Path(vault_root))
    keyring = parse_keyring(external.keyring)
    control = parse_control_record(external.control, keyring=keyring, now=now)
    _verify_registered_attachment(
        Path(vault_root), control.registry_attachment_id
    )
    serving_membership, local_replica_id, membership_path = (
        _load_optional_serving_membership(
            Path(vault_root),
            external=external,
            keyring=keyring,
            control=control,
            now=now,
        )
    )
    return AuthorizationCustody(
        keyring_path=external.keyring_path,
        control_path=external.control_path,
        keyring=keyring,
        control=control,
        serving_membership=serving_membership,
        local_replica_id=local_replica_id,
        membership_path=membership_path,
    )


def _load_optional_serving_membership(
    vault_root: Path,
    *,
    external: ExternalAuthorizationCustody,
    keyring: AuthorizationKeyring,
    control: AuthorizationControlRecord,
    now: int,
) -> tuple[ServingMembershipEpoch | None, str | None, Path | None]:
    """Load session-only fleet state without blocking standing-policy content.

    Enrollment/activation custody remains mandatory for governed content.  Fleet
    membership is a narrower authority used only for session issuance and
    resumption, so an absent or bad record disables that capability rather than
    turning a healthy standing-policy read into a content outage.
    """

    configured_path = os.environ.get(MEMBERSHIP_FILE_ENV, "").strip()
    configured_replica = os.environ.get(REPLICA_ID_ENV, "").strip()
    if not configured_path or not configured_replica:
        return None, None, None
    try:
        membership_path = _configured_external_path(MEMBERSHIP_FILE_ENV, vault_root)
        if membership_path in {external.keyring_path, external.control_path}:
            raise AuthorizationCustodyUnavailable
        loaded = _load_file(membership_path)
        record = authorization_serving_membership.parse_serving_membership(
            loaded.data,
            verifier_keys={item.key_id: item.key for item in keyring.accepted_keys},
            now=now,
            expected_cell_id=control.cell_id,
            expected_logical_vault_id=control.logical_vault_id,
            expected_epoch=control.serving_membership_epoch,
            expected_digest=control.serving_membership_digest,
        )
        replica_id = _bounded_identifier(configured_replica)
        if not any(item.replica_id == replica_id for item in record.replicas):
            raise AuthorizationCustodyUnavailable
        return record, replica_id, membership_path
    except (
        AuthorizationCustodyUnavailable,
        authorization_serving_membership.ServingMembershipUnavailable,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ):
        return None, None, None


def _control_value(record: AuthorizationControlRecord) -> dict[str, object]:
    return {
        "version": record.version,
        "keyring_id": record.keyring_id,
        "cell_id": record.cell_id,
        "logical_vault_id": record.logical_vault_id,
        "registry_attachment_id": record.registry_attachment_id,
        "attachment_epoch": record.attachment_epoch,
        "governance_enrolled": record.governance_enrolled,
        "activation_store_id": record.activation_store_id,
        "activation_epoch": record.activation_epoch,
        "activation_state_digest": record.activation_state_digest,
        "serving_membership_epoch": record.serving_membership_epoch,
        "serving_membership_digest": record.serving_membership_digest,
        "issued_at": record.issued_at,
        "expires_at": record.expires_at,
        "signing_key_id": record.signing_key_id,
    }


def _signed_control_bytes(
    record: AuthorizationControlRecord,
    *,
    signing_key: bytes,
) -> bytes:
    value = _control_value(record)
    value["mac"] = (
        base64.urlsafe_b64encode(
            hmac.new(signing_key, _control_mac_input(value), hashlib.sha256).digest()
        )
        .rstrip(b"=")
        .decode("ascii")
    )
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if not 1 <= len(encoded) <= MAX_CUSTODY_FILE_BYTES:
        raise AuthorizationCustodyUnavailable
    return encoded


def _prepare_private_stage(target_path: Path, staged: held_fs.HeldFile) -> None:
    descriptor = getattr(staged, "descriptor", None)
    if not isinstance(descriptor, int):
        raise AuthorizationCustodyUnavailable
    if os.name == "nt":
        name = getattr(staged, "name", None)
        if not isinstance(name, str) or not name:
            raise AuthorizationCustodyUnavailable
        mutation_lock._windows_apply_private_dacl(
            target_path.parent / name,
            mutation_lock._windows_current_user_sid(),
        )
    else:
        os.fchmod(descriptor, 0o600)
    info = os.fstat(descriptor)
    if not _file_is_owner_protected(descriptor, info):
        raise AuthorizationCustodyUnavailable


def _private_parent_is_safe(path: Path) -> bool:
    try:
        info = os.lstat(path)
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & reparse
        ):
            return False
        if os.name == "nt":  # pragma: no cover - exercised by Windows CI
            sid = mutation_lock._windows_current_user_sid()
            return mutation_lock._windows_private_dacl_is_valid(
                mutation_lock._windows_dacl_sddl(path),
                sid,
                directory=True,
            )
        return int(info.st_uid) == os.geteuid() and not stat.S_IMODE(info.st_mode) & 0o077
    except (OSError, RuntimeError, ValueError):
        return False


def _publish_private_file(path: Path, data: bytes) -> bytes:
    """Create one exact private custody file, or adopt an exact concurrent retry."""

    if not isinstance(data, bytes) or not 1 <= len(data) <= MAX_CUSTODY_FILE_BYTES:
        raise AuthorizationCustodyUnavailable
    parent_path = path.parent
    anchor = parent_path.parent
    if (
        anchor == parent_path
        or not parent_path.name
        or not path.name
        or not _private_parent_is_safe(parent_path)
    ):
        raise AuthorizationCustodyUnavailable
    try:
        acquired = held_fs.acquire(anchor)
        if not acquired.ok:
            raise AuthorizationCustodyUnavailable
        with acquired.require() as filesystem:
            parent_result = filesystem.parent(parent_path.name, access="flush")
            if not parent_result.ok:
                raise AuthorizationCustodyUnavailable
            with parent_result.require() as parent:
                published = held_fs.publish_bytes(
                    filesystem,
                    parent,
                    path.name,
                    data,
                    prepare=lambda staged: _prepare_private_stage(path, staged),
                )
                if not published.ok:
                    if published.error is None or published.error.code != "DESTINATION_EXISTS":
                        raise AuthorizationCustodyUnavailable
                    installed = _load_file(path)
                    if not hmac.compare_digest(installed.data, data):
                        raise AuthorizationCustodyUnavailable
                    return installed.data
                flushed = filesystem.flush_directory(parent)
                if not flushed.ok:
                    raise AuthorizationCustodyUnavailable
        installed = _load_file(path)
        if not hmac.compare_digest(installed.data, data):
            raise AuthorizationCustodyUnavailable
        return installed.data
    except AuthorizationCustodyUnavailable:
        raise
    except (OSError, RuntimeError, TypeError, ValueError, held_fs.HeldFsError):
        raise AuthorizationCustodyUnavailable from None


def _replace_control_bytes(
    path: Path,
    *,
    expected: bytes,
    target: bytes,
) -> None:
    parent_path = path.parent
    anchor = parent_path.parent
    if anchor == parent_path or not parent_path.name or not path.name:
        raise AuthorizationCustodyUnavailable
    try:
        acquired = held_fs.acquire(anchor)
        if not acquired.ok:
            raise AuthorizationCustodyUnavailable
        with acquired.require() as filesystem:
            parent_result = filesystem.parent(parent_path.name, access="flush")
            if not parent_result.ok:
                raise AuthorizationCustodyUnavailable
            with parent_result.require() as parent:
                current_result = filesystem.file(parent, path.name)
                if not current_result.ok:
                    raise AuthorizationCustodyUnavailable
                with current_result.require() as current:
                    observed = filesystem.read(current)
                    if (
                        current.identity.link_count != 1
                        or not observed.ok
                        or not hmac.compare_digest(observed.require(), expected)
                    ):
                        raise AuthorizationCustodyUnavailable
                    identity = current.identity
                published = held_fs.publish_bytes(
                    filesystem,
                    parent,
                    path.name,
                    target,
                    expected_identity=identity,
                    expected_sha256=hashlib.sha256(expected).hexdigest(),
                    prepare=lambda staged: _prepare_private_stage(path, staged),
                )
                if not published.ok:
                    raise AuthorizationCustodyUnavailable
                flushed = filesystem.flush_directory(parent)
                if not flushed.ok:
                    raise AuthorizationCustodyUnavailable
        installed = _load_file(path)
        if not hmac.compare_digest(installed.data, target):
            raise AuthorizationCustodyUnavailable
    except AuthorizationCustodyUnavailable:
        raise
    except (OSError, RuntimeError, TypeError, ValueError, held_fs.HeldFsError):
        raise AuthorizationCustodyUnavailable from None


def _standalone_staging_identifier(
    prefix: str,
    *,
    attachment_id: str,
    key: bytes,
) -> str:
    digest = hmac.new(
        key,
        _framed(
            _STANDALONE_STAGING_DOMAIN,
            (prefix.encode("ascii"), attachment_id.encode("ascii")),
        ),
        hashlib.sha256,
    ).hexdigest()
    return f"{prefix}-{digest}"


def _verify_standalone_staging_keyring(
    keyring: AuthorizationKeyring,
    *,
    attachment_id: str,
) -> None:
    if keyring.version != 1 or len(keyring.accepted_keys) != 1:
        raise AuthorizationCustodyUnavailable
    active_key = keyring.active_key
    expected = (
        _standalone_staging_identifier(
            "auth-key",
            attachment_id=attachment_id,
            key=active_key.key,
        ),
        _standalone_staging_identifier(
            "keyring",
            attachment_id=attachment_id,
            key=active_key.key,
        ),
        _standalone_staging_identifier(
            "cell",
            attachment_id=attachment_id,
            key=active_key.key,
        ),
        _standalone_staging_identifier(
            "vault",
            attachment_id=attachment_id,
            key=active_key.key,
        ),
    )
    observed = (
        active_key.key_id,
        keyring.keyring_id,
        keyring.cell_id,
        keyring.logical_vault_id,
    )
    matches = tuple(
        hmac.compare_digest(actual, required)
        for actual, required in zip(observed, expected, strict=True)
    )
    if not all(matches):
        raise AuthorizationCustodyUnavailable


def _new_standalone_staging_keyring(
    *,
    attachment_id: str,
    current_time: int,
) -> AuthorizationKeyring:
    key_bytes = secrets.token_bytes(32)
    key = AuthorizationVerifierKey(
        key_id=_standalone_staging_identifier(
            "auth-key",
            attachment_id=attachment_id,
            key=key_bytes,
        ),
        key=key_bytes,
        not_before=current_time,
        not_after=current_time + _DEFAULT_KEY_TTL_SECONDS,
    )
    return AuthorizationKeyring(
        version=1,
        keyring_id=_standalone_staging_identifier(
            "keyring",
            attachment_id=attachment_id,
            key=key_bytes,
        ),
        cell_id=_standalone_staging_identifier(
            "cell",
            attachment_id=attachment_id,
            key=key_bytes,
        ),
        logical_vault_id=_standalone_staging_identifier(
            "vault",
            attachment_id=attachment_id,
            key=key_bytes,
        ),
        active_key_id=key.key_id,
        accepted_keys=(key,),
    )


def _keyring_bytes(keyring: AuthorizationKeyring) -> bytes:
    value = {
        "version": keyring.version,
        "keyring_id": keyring.keyring_id,
        "cell_id": keyring.cell_id,
        "logical_vault_id": keyring.logical_vault_id,
        "active_key_id": keyring.active_key_id,
        "accepted_keys": [
            {
                "key_id": item.key_id,
                "key": base64.urlsafe_b64encode(item.key).rstrip(b"=").decode("ascii"),
                "not_before": item.not_before,
                "not_after": item.not_after,
            }
            for item in keyring.accepted_keys
        ],
    }
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if not 1 <= len(encoded) <= MAX_CUSTODY_FILE_BYTES:
        raise AuthorizationCustodyUnavailable
    return encoded


def _standalone_membership_bytes(
    *,
    keyring: AuthorizationKeyring,
    control: AuthorizationControlRecord,
    replica_id: str,
) -> bytes:
    """Build the authenticated singleton form of the fleet record."""

    if control.serving_membership_epoch != 1:
        raise AuthorizationCustodyUnavailable
    accepted_key_ids = tuple(sorted(key.key_id for key in keyring.accepted_keys))
    attestation = authorization_serving_membership.ReplicaReadinessAttestation(
        version=1,
        epoch=control.serving_membership_epoch,
        replica_id=_bounded_identifier(replica_id),
        state="SERVING",
        software_version=runtime_software_version(),
        schema_version=4,
        cell_id=control.cell_id,
        active_key_id=keyring.active_key_id,
        accepted_key_ids=accepted_key_ids,
        control_digest=control_attestation_digest(control),
        keyring_digest=keyring_attestation_digest(keyring),
        attested_at=control.issued_at,
        expires_at=min(
            control.expires_at,
            control.issued_at
            + authorization_serving_membership.MAX_ATTESTATION_TTL_SECONDS,
        ),
        issuance_stopped=False,
        no_in_flight=False,
        signing_key_id=keyring.active_key_id,
    )
    record = authorization_serving_membership.ServingMembershipEpoch(
        version=1,
        epoch=control.serving_membership_epoch,
        cell_id=control.cell_id,
        logical_vault_id=control.logical_vault_id,
        previous_epoch_digest=None,
        issued_at=control.issued_at,
        expires_at=min(
            control.expires_at,
            control.issued_at
            + authorization_serving_membership.MAX_ATTESTATION_TTL_SECONDS,
        ),
        replicas=(attestation,),
        signing_key_id=keyring.active_key_id,
    )
    return authorization_serving_membership.encode_serving_membership(
        record,
        verifier_keys={item.key_id: item.key for item in keyring.accepted_keys},
    )


def _governance_negative_scan(vault_root: Path) -> None:
    try:
        acquired = held_fs.acquire(vault_root)
        if not acquired.ok:
            raise AuthorizationCustodyUnavailable
        with acquired.require() as filesystem:
            kb_result = filesystem.parent("Knowledge Base")
            if not kb_result.ok:
                raise AuthorizationCustodyUnavailable
            with kb_result.require() as knowledge_base:
                governance = filesystem.parent("Knowledge Base/_Governance")
                if governance.ok:
                    governance.require().close()
                    raise AuthorizationCustodyUnavailable
                if governance.error is None or governance.error.code != "MISSING":
                    raise AuthorizationCustodyUnavailable
                for name in _GOVERNANCE_AUTHORITY_NAMES:
                    candidate = filesystem.file(knowledge_base, name)
                    if candidate.ok:
                        candidate.require().close()
                        raise AuthorizationCustodyUnavailable
                    if candidate.error is None or candidate.error.code != "MISSING":
                        raise AuthorizationCustodyUnavailable
                if not filesystem.validate_directory(knowledge_base).ok:
                    raise AuthorizationCustodyUnavailable
    except AuthorizationCustodyUnavailable:
        raise
    except (OSError, RuntimeError, ValueError, held_fs.HeldFsError):
        raise AuthorizationCustodyUnavailable from None


def _require_exact_v3_authority(vault_root: Path) -> None:
    from . import schema_v4, store

    connection = store.open_readonly_connection(vault_root)
    if connection is None:
        raise AuthorizationCustodyUnavailable
    try:
        schema_v4.require_exact_v3_connection(connection)
    except (schema_v4.SchemaV4Error, OSError, RuntimeError):
        raise AuthorizationCustodyUnavailable from None
    finally:
        connection.close()


def _provisioning_result(
    custody: AuthorizationCustody,
) -> StandaloneProvisioningResult:
    if (
        custody.local_replica_id is None
        or custody.serving_membership is None
        or custody.membership_path is None
    ):
        raise AuthorizationCustodyUnavailable
    return StandaloneProvisioningResult(
        keyring_path=custody.keyring_path,
        control_path=custody.control_path,
        membership_path=custody.membership_path,
        keyring_id=custody.keyring.keyring_id,
        cell_id=custody.keyring.cell_id,
        logical_vault_id=custody.keyring.logical_vault_id,
        registry_attachment_id=custody.control.registry_attachment_id,
        attachment_epoch=custody.control.attachment_epoch,
        replica_id=custody.local_replica_id,
    )


def stage_standalone_v3_custody(
    vault_root: Path,
    *,
    now: int,
) -> StandaloneV3StagingResult:
    """Stage inert external identity for an exact-v3 offline migration.

    This explicit owner operation publishes only the keyring.  It does not
    create a control or membership record, does not enrol governance, and
    cannot make authorization-session service ready.  A later coordinator
    must bind this identity to one exact initial v4 tuple under the full
    schema/replica fence before it may migrate the sidecar.
    """

    root = Path(vault_root)
    current_time = _bounded_time(now)
    if current_time > _MAX_SIGNED_SQLITE_INTEGER - _DEFAULT_KEY_TTL_SECONDS:
        raise AuthorizationCustodyUnavailable
    attachment_id = standalone_attachment_id(root)
    keyring_path = _configured_external_path(KEYRING_FILE_ENV, root)
    control_path = _configured_external_path(CONTROL_FILE_ENV, root)
    membership_path = _configured_external_path(MEMBERSHIP_FILE_ENV, root)
    if len({keyring_path, control_path, membership_path}) != 3:
        raise AuthorizationCustodyUnavailable

    from .. import reserved_paths

    with reserved_paths._identity_coordination_scope(root):
        _require_exact_v3_authority(root)
        for registered_path in (control_path, membership_path):
            try:
                registered = _load_file(registered_path)
            except AuthorizationCustodyUnavailable:
                if os.path.lexists(registered_path):
                    raise
            else:
                if registered is not None:
                    raise AuthorizationCustodyUnavailable

        try:
            loaded = _load_file(keyring_path)
        except AuthorizationCustodyUnavailable:
            if os.path.lexists(keyring_path):
                raise
            loaded = None
        if loaded is None:
            keyring = _new_standalone_staging_keyring(
                attachment_id=attachment_id,
                current_time=current_time,
            )
            _publish_private_file(keyring_path, _keyring_bytes(keyring))
        else:
            keyring = parse_keyring(loaded.data)

        _verify_standalone_staging_keyring(
            keyring,
            attachment_id=attachment_id,
        )
        if not keyring.active_key.not_before <= current_time < keyring.active_key.not_after:
            raise AuthorizationCustodyUnavailable
        _require_exact_v3_authority(root)

    return StandaloneV3StagingResult(
        keyring_path=keyring_path,
        keyring_id=keyring.keyring_id,
        cell_id=keyring.cell_id,
        logical_vault_id=keyring.logical_vault_id,
        registry_attachment_id=attachment_id,
        attachment_epoch=1,
    )


def enroll_standalone_v3_migration(
    vault_root: Path,
    *,
    target: VerifiedActiveGovernanceState,
    now: int,
) -> ActivationRegistryAcknowledgement:
    """Bind staged exact-v3 custody to one irreversible initial v4 tuple.

    The sidecar remains exact v3 throughout this operation.  Publishing the
    enrolled control record first deliberately turns the crash interval into a
    fail-closed state; an exact retry may then restore the precomputed serving
    membership without rewriting identity or activation authority.  A later
    offline coordinator owns the actual transactional v3-to-v4 migration.
    """

    from . import schema_v4

    if (
        not isinstance(target, schema_v4.VerifiedActiveGovernanceState)
        or target.activation_epoch != 1
    ):
        raise AuthorizationCustodyUnavailable
    _bounded_identifier(target.logical_vault_id)
    _bounded_identifier(target.activation_store_id)
    _sha256_hex(target.activation_state_digest)

    root = Path(vault_root)
    current_time = _bounded_time(now)
    attachment_id = standalone_attachment_id(root)
    keyring_path = _configured_external_path(KEYRING_FILE_ENV, root)
    control_path = _configured_external_path(CONTROL_FILE_ENV, root)
    membership_path = _configured_external_path(MEMBERSHIP_FILE_ENV, root)
    replica_id = _bounded_identifier(os.environ.get(REPLICA_ID_ENV, ""))
    if len({keyring_path, control_path, membership_path}) != 3:
        raise AuthorizationCustodyUnavailable

    acknowledgement = schema_v4.ActivationRegistryAcknowledgement(
        activation_store_id=target.activation_store_id,
        activation_epoch=target.activation_epoch,
        activation_state_digest=target.activation_state_digest,
    )

    from .. import reserved_paths

    with reserved_paths._identity_coordination_scope(root):
        _require_exact_v3_authority(root)
        keyring = parse_keyring(_load_file(keyring_path).data)
        _verify_standalone_staging_keyring(
            keyring,
            attachment_id=attachment_id,
        )
        if (
            target.logical_vault_id != keyring.logical_vault_id
            or not keyring.active_key.not_before
            <= current_time
            < keyring.active_key.not_after
        ):
            raise AuthorizationCustodyUnavailable

        control_loaded: _LoadedCustodyFile | None = None
        membership_loaded: _LoadedCustodyFile | None = None
        try:
            control_loaded = _load_file(control_path)
        except AuthorizationCustodyUnavailable:
            if os.path.lexists(control_path):
                raise
        try:
            membership_loaded = _load_file(membership_path)
        except AuthorizationCustodyUnavailable:
            if os.path.lexists(membership_path):
                raise
        if membership_loaded is not None and control_loaded is None:
            raise AuthorizationCustodyUnavailable

        if control_loaded is None:
            provisional_control = AuthorizationControlRecord(
                version=1,
                keyring_id=keyring.keyring_id,
                cell_id=keyring.cell_id,
                logical_vault_id=keyring.logical_vault_id,
                registry_attachment_id=attachment_id,
                attachment_epoch=1,
                governance_enrolled=True,
                activation_store_id=target.activation_store_id,
                activation_epoch=target.activation_epoch,
                activation_state_digest=target.activation_state_digest,
                serving_membership_epoch=1,
                serving_membership_digest="0" * 64,
                issued_at=current_time,
                expires_at=keyring.active_key.not_after,
                signing_key_id=keyring.active_key_id,
            )
            encoded_membership = _standalone_membership_bytes(
                keyring=keyring,
                control=provisional_control,
                replica_id=replica_id,
            )
            control = replace(
                provisional_control,
                serving_membership_digest=(
                    authorization_serving_membership.serving_membership_digest(
                        encoded_membership
                    )
                ),
            )
            _require_exact_v3_authority(root)
            _publish_private_file(
                control_path,
                _signed_control_bytes(
                    control,
                    signing_key=keyring.active_key.key,
                ),
            )
        else:
            control = parse_control_record(
                control_loaded.data,
                keyring=keyring,
                now=current_time,
            )
            _verify_registered_attachment(root, control.registry_attachment_id)
            if (
                control.registry_attachment_id != attachment_id
                or control.attachment_epoch != 1
                or not control.governance_enrolled
                or control.activation_store_id != target.activation_store_id
                or control.activation_epoch != target.activation_epoch
                or control.activation_state_digest != target.activation_state_digest
                or control.serving_membership_epoch != 1
            ):
                raise AuthorizationCustodyUnavailable
            encoded_membership = _standalone_membership_bytes(
                keyring=keyring,
                control=control,
                replica_id=replica_id,
            )
            if (
                authorization_serving_membership.serving_membership_digest(
                    encoded_membership
                )
                != control.serving_membership_digest
            ):
                raise AuthorizationCustodyUnavailable

        if membership_loaded is None:
            _publish_private_file(membership_path, encoded_membership)
        elif not hmac.compare_digest(membership_loaded.data, encoded_membership):
            raise AuthorizationCustodyUnavailable
        _require_exact_v3_authority(root)

    verified = load_authorization_custody(root, now=current_time)
    if (
        not verified.control.governance_enrolled
        or verified.control.activation_store_id != target.activation_store_id
        or verified.control.activation_epoch != target.activation_epoch
        or verified.control.activation_state_digest != target.activation_state_digest
        or verified.serving_membership is None
    ):
        raise AuthorizationCustodyUnavailable
    return acknowledgement


def provision_standalone_custody(
    vault_root: Path,
    *,
    now: int,
) -> StandaloneProvisioningResult:
    """Explicitly provision one never-enrolled standalone attachment.

    This is never called from an ordinary loader.  It creates no vault state;
    the externally authenticated control and serving-membership records are
    the final registration points, and an interrupted publication can be
    retried without rotating the generated identity.
    """

    root = Path(vault_root)
    current_time = _bounded_time(now)
    if current_time > _MAX_SIGNED_SQLITE_INTEGER - _DEFAULT_KEY_TTL_SECONDS:
        raise AuthorizationCustodyUnavailable
    attachment_id = standalone_attachment_id(root)
    keyring_path = _configured_external_path(KEYRING_FILE_ENV, root)
    control_path = _configured_external_path(CONTROL_FILE_ENV, root)
    membership_path = _configured_external_path(MEMBERSHIP_FILE_ENV, root)
    replica_id = _bounded_identifier(os.environ.get(REPLICA_ID_ENV, ""))
    if len({keyring_path, control_path, membership_path}) != 3:
        raise AuthorizationCustodyUnavailable

    from .. import reserved_paths

    # First attachment is the one operation that must prove the vault has no
    # private owner state at all.  Take the registry-wide identity guard rather
    # than pretending to hold dispatcher authority for two different owners.
    with reserved_paths._identity_coordination_scope(root):
        _governance_negative_scan(root)
        keyring_loaded: _LoadedCustodyFile | None = None
        control_loaded: _LoadedCustodyFile | None = None
        membership_loaded: _LoadedCustodyFile | None = None
        try:
            keyring_loaded = _load_file(keyring_path)
        except AuthorizationCustodyUnavailable:
            if keyring_path.exists():
                raise
        try:
            control_loaded = _load_file(control_path)
        except AuthorizationCustodyUnavailable:
            if control_path.exists():
                raise
        try:
            membership_loaded = _load_file(membership_path)
        except AuthorizationCustodyUnavailable:
            if membership_path.exists():
                raise

        if (
            (control_loaded is not None and keyring_loaded is None)
            or (membership_loaded is not None and control_loaded is None)
        ):
            raise AuthorizationCustodyUnavailable
        if keyring_loaded is not None:
            keyring = parse_keyring(keyring_loaded.data)
        else:
            keyring = _new_standalone_staging_keyring(
                attachment_id=attachment_id,
                current_time=current_time,
            )
            encoded_keyring = _keyring_bytes(keyring)
            _publish_private_file(keyring_path, encoded_keyring)

        if not keyring.active_key.not_before <= current_time < keyring.active_key.not_after:
            raise AuthorizationCustodyUnavailable
        if control_loaded is not None:
            existing_control = parse_control_record(
                control_loaded.data,
                keyring=keyring,
                now=current_time,
            )
            if existing_control.registry_attachment_id != attachment_id:
                raise AuthorizationCustodyUnavailable
        if control_loaded is None:
            _verify_standalone_staging_keyring(
                keyring,
                attachment_id=attachment_id,
            )
            provisional_control = AuthorizationControlRecord(
                version=1,
                keyring_id=keyring.keyring_id,
                cell_id=keyring.cell_id,
                logical_vault_id=keyring.logical_vault_id,
                registry_attachment_id=attachment_id,
                attachment_epoch=1,
                governance_enrolled=False,
                activation_store_id=None,
                activation_epoch=None,
                activation_state_digest=None,
                serving_membership_epoch=1,
                serving_membership_digest="0" * 64,
                issued_at=current_time,
                expires_at=keyring.active_key.not_after,
                signing_key_id=keyring.active_key_id,
            )
            encoded_membership = _standalone_membership_bytes(
                keyring=keyring,
                control=provisional_control,
                replica_id=replica_id,
            )
            control = replace(
                provisional_control,
                serving_membership_digest=(
                    authorization_serving_membership.serving_membership_digest(
                        encoded_membership
                    )
                ),
            )
            encoded_control = _signed_control_bytes(
                control,
                signing_key=keyring.active_key.key,
            )
            _governance_negative_scan(root)
            _publish_private_file(control_path, encoded_control)
            _publish_private_file(membership_path, encoded_membership)
        else:
            control = parse_control_record(
                control_loaded.data,
                keyring=keyring,
                now=current_time,
            )
            if control.serving_membership_epoch != 1:
                raise AuthorizationCustodyUnavailable
            encoded_membership = _standalone_membership_bytes(
                keyring=keyring,
                control=control,
                replica_id=replica_id,
            )
            if (
                authorization_serving_membership.serving_membership_digest(
                    encoded_membership
                )
                != control.serving_membership_digest
            ):
                raise AuthorizationCustodyUnavailable
            if membership_loaded is None:
                _publish_private_file(membership_path, encoded_membership)
            elif not hmac.compare_digest(membership_loaded.data, encoded_membership):
                raise AuthorizationCustodyUnavailable

    return _provisioning_result(load_authorization_custody(root, now=current_time))


def enroll_initial_activation_tuple(
    vault_root: Path,
    *,
    expected_control: AuthorizationControlRecord,
    target: VerifiedActiveGovernanceState,
    now: int,
) -> ActivationRegistryAcknowledgement:
    """Irreversibly CAS never-enrolled custody to one exact initial tuple."""

    from . import schema_v4

    if (
        not isinstance(expected_control, AuthorizationControlRecord)
        or not isinstance(target, schema_v4.VerifiedActiveGovernanceState)
        or expected_control.governance_enrolled
        or expected_control.activation_store_id is not None
        or expected_control.activation_epoch is not None
        or expected_control.activation_state_digest is not None
        or target.logical_vault_id != expected_control.logical_vault_id
        or target.activation_epoch != 1
    ):
        raise AuthorizationCustodyUnavailable
    _bounded_identifier(target.activation_store_id)
    _sha256_hex(target.activation_state_digest)

    root = Path(vault_root)
    from .. import reserved_paths

    with reserved_paths._identity_coordination_scope(root):
        external = load_external_custody(root)
        keyring = parse_keyring(external.keyring)
        current = parse_control_record(external.control, keyring=keyring, now=now)
        _verify_registered_attachment(root, current.registry_attachment_id)
        target_control = AuthorizationControlRecord(
            version=expected_control.version,
            keyring_id=expected_control.keyring_id,
            cell_id=expected_control.cell_id,
            logical_vault_id=expected_control.logical_vault_id,
            registry_attachment_id=expected_control.registry_attachment_id,
            attachment_epoch=expected_control.attachment_epoch,
            governance_enrolled=True,
            activation_store_id=target.activation_store_id,
            activation_epoch=target.activation_epoch,
            activation_state_digest=target.activation_state_digest,
            serving_membership_epoch=expected_control.serving_membership_epoch,
            serving_membership_digest=expected_control.serving_membership_digest,
            issued_at=expected_control.issued_at,
            expires_at=expected_control.expires_at,
            signing_key_id=expected_control.signing_key_id,
        )
        acknowledgement = schema_v4.ActivationRegistryAcknowledgement(
            activation_store_id=target.activation_store_id,
            activation_epoch=target.activation_epoch,
            activation_state_digest=target.activation_state_digest,
        )
        if current == target_control:
            return acknowledgement
        if current != expected_control:
            raise AuthorizationCustodyUnavailable
        _governance_negative_scan(root)
        signing_key = next(
            (
                item.key
                for item in keyring.accepted_keys
                if item.key_id == current.signing_key_id
            ),
            None,
        )
        if signing_key is None:
            raise AuthorizationCustodyUnavailable
        _replace_control_bytes(
            external.control_path,
            expected=external.control,
            target=_signed_control_bytes(target_control, signing_key=signing_key),
        )
    verified = load_authorization_custody(root, now=now)
    if verified.control != target_control:
        raise AuthorizationCustodyUnavailable
    return acknowledgement


def acknowledge_activation_tuple(
    vault_root: Path,
    *,
    expected_control: AuthorizationControlRecord,
    target: VerifiedActiveGovernanceState,
    now: int,
) -> ActivationRegistryAcknowledgement:
    """CAS the protected external registry to one exact committed successor."""

    from . import schema_v4

    if (
        not isinstance(expected_control, AuthorizationControlRecord)
        or not isinstance(target, schema_v4.VerifiedActiveGovernanceState)
        or not expected_control.governance_enrolled
        or expected_control.activation_store_id is None
        or expected_control.activation_epoch is None
        or expected_control.activation_state_digest is None
        or target.logical_vault_id != expected_control.logical_vault_id
        or target.activation_store_id != expected_control.activation_store_id
        or target.activation_epoch != expected_control.activation_epoch + 1
    ):
        raise AuthorizationCustodyUnavailable

    root = Path(vault_root)
    external = load_external_custody(root)
    keyring = parse_keyring(external.keyring)
    current = parse_control_record(external.control, keyring=keyring, now=now)
    _verify_registered_attachment(root, current.registry_attachment_id)
    target_control = AuthorizationControlRecord(
        version=expected_control.version,
        keyring_id=expected_control.keyring_id,
        cell_id=expected_control.cell_id,
        logical_vault_id=expected_control.logical_vault_id,
        registry_attachment_id=expected_control.registry_attachment_id,
        attachment_epoch=expected_control.attachment_epoch,
        governance_enrolled=True,
        activation_store_id=target.activation_store_id,
        activation_epoch=target.activation_epoch,
        activation_state_digest=target.activation_state_digest,
        serving_membership_epoch=expected_control.serving_membership_epoch,
        serving_membership_digest=expected_control.serving_membership_digest,
        issued_at=expected_control.issued_at,
        expires_at=expected_control.expires_at,
        signing_key_id=expected_control.signing_key_id,
    )
    acknowledgement = schema_v4.ActivationRegistryAcknowledgement(
        activation_store_id=target.activation_store_id,
        activation_epoch=target.activation_epoch,
        activation_state_digest=target.activation_state_digest,
    )
    if current == target_control:
        return acknowledgement
    if current != expected_control:
        raise AuthorizationCustodyUnavailable
    signing_key = next(
        (
            key.key
            for key in keyring.accepted_keys
            if key.key_id == target_control.signing_key_id
        ),
        None,
    )
    if signing_key is None:
        raise AuthorizationCustodyUnavailable
    encoded = _signed_control_bytes(target_control, signing_key=signing_key)
    _replace_control_bytes(
        external.control_path,
        expected=external.control,
        target=encoded,
    )
    verified = load_authorization_custody(root, now=now)
    if verified.control != target_control:
        raise AuthorizationCustodyUnavailable
    return acknowledgement
