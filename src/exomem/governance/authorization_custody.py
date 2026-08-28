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
HOSTED_CUSTODY_ROOT = Path("/run/exomem/authorization-session")
HOSTED_KEYRING_FILE = HOSTED_CUSTODY_ROOT / "keyring.json"
HOSTED_CONTROL_FILE = HOSTED_CUSTODY_ROOT / "control.json"
HOSTED_MEMBERSHIP_FILE = HOSTED_CUSTODY_ROOT / "serving-membership.json"
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
_DETACH_ACK_MAC_DOMAIN = b"exomem.authorization-session.detach-ack/v1"
_HOST_REGISTRY_MAC_DOMAIN = b"exomem.authorization-session.host-registry/v1"
_STANDALONE_STAGING_DOMAIN = b"exomem.authorization-session.standalone-staging/v1"
_STANDALONE_CLONE_RECEIPT_DOMAIN = b"exomem.authorization-session.standalone-clone-receipt/v1"
_SHA256_HEX = re.compile(r"[0-9a-f]{64}\Z")
_STANDALONE_ATTACHMENT = re.compile(r"attachment-v1-[0-9a-f]{64}\Z")
_DEFAULT_KEY_TTL_SECONDS = 366 * 24 * 60 * 60
_HOST_REGISTRY_CLOCK_SKEW_SECONDS = 30
_HOST_CONTROL_KEY_BYTES = 32
_HOST_CONTROL_KEY_NAME = "authorization-attachment-host-key-v1"
_GOVERNANCE_AUTHORITY_NAMES = (
    ".governance.sqlite",
    ".governance.sqlite-wal",
    ".governance.sqlite-shm",
    ".governance.sqlite-journal",
)
_DETACH_ACK_FIELDS = frozenset(
    {
        "version",
        "cell_id",
        "logical_vault_id",
        "keyring_id",
        "source_registry_attachment_id",
        "target_registry_attachment_id",
        "source_attachment_epoch",
        "target_attachment_epoch",
        "source_membership_epoch",
        "source_membership_digest",
        "source_control_digest",
        "issued_at",
        "expires_at",
        "signing_key_id",
        "mac",
    }
)
_HOST_REGISTRY_FIELDS = frozenset(
    {
        "version",
        "cell_id",
        "logical_vault_id",
        "keyring_id",
        "registry_attachment_id",
        "attachment_epoch",
        "serving_membership_epoch",
        "serving_membership_digest",
        "state",
        "no_in_flight",
        "updated_at",
        "signing_key_id",
        "mac",
    }
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
    staged_at: int


@dataclass(frozen=True, slots=True)
class _StandaloneDetachAcknowledgement:
    version: int
    cell_id: str
    logical_vault_id: str
    keyring_id: str
    source_registry_attachment_id: str
    target_registry_attachment_id: str
    source_attachment_epoch: int
    target_attachment_epoch: int
    source_membership_epoch: int
    source_membership_digest: str
    source_control_digest: str
    issued_at: int
    expires_at: int
    signing_key_id: str


@dataclass(frozen=True, slots=True)
class _StandaloneHostRegistryRecord:
    version: int
    cell_id: str
    logical_vault_id: str
    keyring_id: str
    registry_attachment_id: str
    attachment_epoch: int
    serving_membership_epoch: int
    serving_membership_digest: str
    state: str
    no_in_flight: bool
    updated_at: int
    signing_key_id: str


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


def _read_retained(
    descriptor: int,
    expected_size: int,
    *,
    maximum_bytes: int = MAX_CUSTODY_FILE_BYTES,
) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    remaining = maximum_bytes + 1
    while remaining:
        chunk = os.read(descriptor, min(remaining, 4096))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    data = b"".join(chunks)
    if len(data) != expected_size or len(data) > maximum_bytes:
        raise AuthorizationCustodyUnavailable
    return data


def _load_file(path: Path) -> _LoadedCustodyFile:
    return _load_private_artifact(path, maximum_bytes=MAX_CUSTODY_FILE_BYTES)


def _load_private_artifact(
    path: Path,
    *,
    maximum_bytes: int,
) -> _LoadedCustodyFile:
    """Load one bounded owner-only external artifact through a retained handle."""

    if maximum_bytes < 1:
        raise AuthorizationCustodyUnavailable
    held: mutation_lock.RetainedRegularFile | None = None
    try:
        held = mutation_lock.retain_regular_file(path)
        before = os.fstat(held.fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or not 1 <= before.st_size <= maximum_bytes
            or not _file_is_owner_protected(held.fd, before)
        ):
            raise AuthorizationCustodyUnavailable
        data = _read_retained(
            held.fd,
            before.st_size,
            maximum_bytes=maximum_bytes,
        )
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
    custody = AuthorizationCustody(
        keyring_path=external.keyring_path,
        control_path=external.control_path,
        keyring=keyring,
        control=control,
        serving_membership=serving_membership,
        local_replica_id=local_replica_id,
        membership_path=membership_path,
    )
    require_current_standalone_registry(
        custody,
        now=now,
        require_serving=False,
    )
    return custody


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


def _detach_ack_value(
    acknowledgement: _StandaloneDetachAcknowledgement,
) -> dict[str, object]:
    return {
        "version": acknowledgement.version,
        "cell_id": acknowledgement.cell_id,
        "logical_vault_id": acknowledgement.logical_vault_id,
        "keyring_id": acknowledgement.keyring_id,
        "source_registry_attachment_id": (
            acknowledgement.source_registry_attachment_id
        ),
        "target_registry_attachment_id": (
            acknowledgement.target_registry_attachment_id
        ),
        "source_attachment_epoch": acknowledgement.source_attachment_epoch,
        "target_attachment_epoch": acknowledgement.target_attachment_epoch,
        "source_membership_epoch": acknowledgement.source_membership_epoch,
        "source_membership_digest": acknowledgement.source_membership_digest,
        "source_control_digest": acknowledgement.source_control_digest,
        "issued_at": acknowledgement.issued_at,
        "expires_at": acknowledgement.expires_at,
        "signing_key_id": acknowledgement.signing_key_id,
    }


def _detach_ack_mac_input(value: dict[str, object]) -> bytes:
    return _framed(
        _DETACH_ACK_MAC_DOMAIN,
        tuple(
            str(value[name]).encode("utf-8")
            for name in (
                "version",
                "cell_id",
                "logical_vault_id",
                "keyring_id",
                "source_registry_attachment_id",
                "target_registry_attachment_id",
                "source_attachment_epoch",
                "target_attachment_epoch",
                "source_membership_epoch",
                "source_membership_digest",
                "source_control_digest",
                "issued_at",
                "expires_at",
                "signing_key_id",
            )
        ),
    )


def _signed_detach_ack_bytes(
    acknowledgement: _StandaloneDetachAcknowledgement,
    *,
    signing_key: bytes,
) -> bytes:
    value = _detach_ack_value(acknowledgement)
    value["mac"] = (
        base64.urlsafe_b64encode(
            hmac.new(
                signing_key,
                _detach_ack_mac_input(value),
                hashlib.sha256,
            ).digest()
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


def _parse_detach_ack(
    raw: bytes,
    *,
    keyring: AuthorizationKeyring,
    now: int,
    allow_expired: bool = False,
) -> _StandaloneDetachAcknowledgement:
    try:
        if not isinstance(raw, bytes) or not 1 <= len(raw) <= MAX_CUSTODY_FILE_BYTES:
            raise AuthorizationCustodyUnavailable
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_closed_object,
        )
        if not isinstance(value, dict) or set(value) != _DETACH_ACK_FIELDS:
            raise AuthorizationCustodyUnavailable
        version = value["version"]
        if version != 1 or isinstance(version, bool):
            raise AuthorizationCustodyUnavailable
        source_attachment = _bounded_identifier(
            value["source_registry_attachment_id"]
        )
        target_attachment = _bounded_identifier(
            value["target_registry_attachment_id"]
        )
        if (
            _STANDALONE_ATTACHMENT.fullmatch(source_attachment) is None
            or _STANDALONE_ATTACHMENT.fullmatch(target_attachment) is None
            or hmac.compare_digest(source_attachment, target_attachment)
        ):
            raise AuthorizationCustodyUnavailable
        source_attachment_epoch = _bounded_time(value["source_attachment_epoch"])
        target_attachment_epoch = _bounded_time(value["target_attachment_epoch"])
        source_membership_epoch = _bounded_time(value["source_membership_epoch"])
        issued_at = _bounded_time(value["issued_at"])
        expires_at = _bounded_time(value["expires_at"])
        current_time = _bounded_time(now)
        if (
            target_attachment_epoch != source_attachment_epoch + 1
            or current_time < issued_at
            or (not allow_expired and current_time >= expires_at)
            or expires_at - issued_at
            > authorization_serving_membership.MAX_ATTESTATION_TTL_SECONDS
        ):
            raise AuthorizationCustodyUnavailable
        signing_key_id = _bounded_identifier(value["signing_key_id"])
        signing_key = next(
            (
                item
                for item in keyring.accepted_keys
                if item.key_id == signing_key_id
            ),
            None,
        )
        if (
            signing_key is None
            or not signing_key.not_before <= current_time < signing_key.not_after
            or not hmac.compare_digest(
                _decode_key(value["mac"]),
                hmac.new(
                    signing_key.key,
                    _detach_ack_mac_input(value),
                    hashlib.sha256,
                ).digest(),
            )
        ):
            raise AuthorizationCustodyUnavailable
        acknowledgement = _StandaloneDetachAcknowledgement(
            version=1,
            cell_id=_bounded_identifier(value["cell_id"]),
            logical_vault_id=_bounded_identifier(value["logical_vault_id"]),
            keyring_id=_bounded_identifier(value["keyring_id"]),
            source_registry_attachment_id=source_attachment,
            target_registry_attachment_id=target_attachment,
            source_attachment_epoch=source_attachment_epoch,
            target_attachment_epoch=target_attachment_epoch,
            source_membership_epoch=source_membership_epoch,
            source_membership_digest=_sha256_hex(value["source_membership_digest"]),
            source_control_digest=_sha256_hex(value["source_control_digest"]),
            issued_at=issued_at,
            expires_at=expires_at,
            signing_key_id=signing_key_id,
        )
        if not hmac.compare_digest(
            _signed_detach_ack_bytes(
                acknowledgement,
                signing_key=signing_key.key,
            ),
            raw,
        ):
            raise AuthorizationCustodyUnavailable
        return acknowledgement
    except AuthorizationCustodyUnavailable:
        raise
    except (
        AttributeError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
    ):
        raise AuthorizationCustodyUnavailable from None


def _host_registry_value(
    record: _StandaloneHostRegistryRecord,
) -> dict[str, object]:
    return {
        "version": record.version,
        "cell_id": record.cell_id,
        "logical_vault_id": record.logical_vault_id,
        "keyring_id": record.keyring_id,
        "registry_attachment_id": record.registry_attachment_id,
        "attachment_epoch": record.attachment_epoch,
        "serving_membership_epoch": record.serving_membership_epoch,
        "serving_membership_digest": record.serving_membership_digest,
        "state": record.state,
        "no_in_flight": record.no_in_flight,
        "updated_at": record.updated_at,
        "signing_key_id": record.signing_key_id,
    }


def _host_registry_mac_input(value: dict[str, object]) -> bytes:
    return _framed(
        _HOST_REGISTRY_MAC_DOMAIN,
        tuple(
            str(value[name]).encode("utf-8")
            for name in (
                "version",
                "cell_id",
                "logical_vault_id",
                "keyring_id",
                "registry_attachment_id",
                "attachment_epoch",
                "serving_membership_epoch",
                "serving_membership_digest",
                "state",
                "no_in_flight",
                "updated_at",
                "signing_key_id",
            )
        ),
    )


def _signed_host_registry_bytes(
    record: _StandaloneHostRegistryRecord,
    *,
    signing_key: bytes,
) -> bytes:
    value = _host_registry_value(record)
    value["mac"] = (
        base64.urlsafe_b64encode(
            hmac.new(
                signing_key,
                _host_registry_mac_input(value),
                hashlib.sha256,
            ).digest()
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


def _parse_host_registry(
    raw: bytes,
    *,
    host_key: bytes,
    now: int,
) -> _StandaloneHostRegistryRecord:
    try:
        if not isinstance(raw, bytes) or not 1 <= len(raw) <= MAX_CUSTODY_FILE_BYTES:
            raise AuthorizationCustodyUnavailable
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_closed_object,
        )
        if not isinstance(value, dict) or set(value) != _HOST_REGISTRY_FIELDS:
            raise AuthorizationCustodyUnavailable
        version = value["version"]
        if version != 1 or isinstance(version, bool):
            raise AuthorizationCustodyUnavailable
        state = value["state"]
        no_in_flight = value["no_in_flight"]
        if (
            state not in {"SERVING", "DRAINING"}
            or not isinstance(no_in_flight, bool)
            or (state == "SERVING" and no_in_flight)
        ):
            raise AuthorizationCustodyUnavailable
        updated_at = _bounded_time(value["updated_at"])
        current_time = _bounded_time(now)
        if updated_at > current_time + _HOST_REGISTRY_CLOCK_SKEW_SECONDS:
            raise AuthorizationCustodyUnavailable
        signing_key_id = _bounded_identifier(value["signing_key_id"])
        if (
            not isinstance(host_key, bytes)
            or len(host_key) != _HOST_CONTROL_KEY_BYTES
            or not hmac.compare_digest(signing_key_id, _host_control_key_id(host_key))
            or not hmac.compare_digest(
                _decode_key(value["mac"]),
                hmac.new(
                    host_key,
                    _host_registry_mac_input(value),
                    hashlib.sha256,
                ).digest(),
            )
        ):
            raise AuthorizationCustodyUnavailable
        attachment_id = _bounded_identifier(value["registry_attachment_id"])
        if _STANDALONE_ATTACHMENT.fullmatch(attachment_id) is None:
            raise AuthorizationCustodyUnavailable
        record = _StandaloneHostRegistryRecord(
            version=1,
            cell_id=_bounded_identifier(value["cell_id"]),
            logical_vault_id=_bounded_identifier(value["logical_vault_id"]),
            keyring_id=_bounded_identifier(value["keyring_id"]),
            registry_attachment_id=attachment_id,
            attachment_epoch=_bounded_time(value["attachment_epoch"]),
            serving_membership_epoch=_bounded_time(
                value["serving_membership_epoch"]
            ),
            serving_membership_digest=_sha256_hex(
                value["serving_membership_digest"]
            ),
            state=str(state),
            no_in_flight=no_in_flight,
            updated_at=updated_at,
            signing_key_id=signing_key_id,
        )
        if not hmac.compare_digest(
            _signed_host_registry_bytes(record, signing_key=host_key),
            raw,
        ):
            raise AuthorizationCustodyUnavailable
        return record
    except AuthorizationCustodyUnavailable:
        raise
    except (
        AttributeError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
    ):
        raise AuthorizationCustodyUnavailable from None


def _windows_program_data_root() -> Path:
    """Resolve ProgramData from the OS known-folder service, never the environment."""

    if os.name != "nt":
        raise AuthorizationCustodyUnavailable
    try:  # pragma: no cover - exercised by Windows CI
        import ctypes
        from ctypes import wintypes

        class _Guid(ctypes.Structure):
            _fields_ = (
                ("data1", wintypes.DWORD),
                ("data2", wintypes.WORD),
                ("data3", wintypes.WORD),
                ("data4", ctypes.c_ubyte * 8),
            )

        program_data = _Guid(
            0x62AB5D82,
            0xFDC1,
            0x4DC3,
            (ctypes.c_ubyte * 8)(0xA9, 0xDD, 0x07, 0x0D, 0x1D, 0x49, 0x5D, 0x97),
        )
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        ole32 = ctypes.WinDLL("ole32", use_last_error=True)
        shell32.SHGetKnownFolderPath.argtypes = (
            ctypes.POINTER(_Guid),
            wintypes.DWORD,
            wintypes.HANDLE,
            ctypes.POINTER(ctypes.c_wchar_p),
        )
        shell32.SHGetKnownFolderPath.restype = ctypes.c_long
        ole32.CoTaskMemFree.argtypes = (ctypes.c_void_p,)
        ole32.CoTaskMemFree.restype = None
        resolved = ctypes.c_wchar_p()
        result = shell32.SHGetKnownFolderPath(
            ctypes.byref(program_data),
            0,
            None,
            ctypes.byref(resolved),
        )
        try:
            if result != 0 or not resolved.value:
                raise AuthorizationCustodyUnavailable
            root = Path(resolved.value)
        finally:
            if resolved:
                ole32.CoTaskMemFree(ctypes.cast(resolved, ctypes.c_void_p))
        if not root.is_absolute():
            raise AuthorizationCustodyUnavailable
        return root
    except AuthorizationCustodyUnavailable:
        raise
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
        raise AuthorizationCustodyUnavailable from None


def _standalone_host_control_root() -> Path:
    """Return the non-configurable OS account/machine host-control root."""

    if os.name == "nt":  # pragma: no cover - exercised by Windows CI
        return _windows_program_data_root() / "exomem" / "standalone-host-control-v1"
    try:
        import pwd

        home = Path(pwd.getpwuid(os.geteuid()).pw_dir)
    except (KeyError, OSError, RuntimeError, TypeError, ValueError):
        raise AuthorizationCustodyUnavailable from None
    if not home.is_absolute():
        raise AuthorizationCustodyUnavailable
    return home / ".local" / "state" / "exomem" / "standalone-host-control-v1"


def _host_registry_path(logical_vault_id: str) -> Path:

    identity = _bounded_identifier(logical_vault_id)
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return (
        _standalone_host_control_root()
        / "authorization-attachment-registry"
        / f"{digest}.json"
    )


def _prepare_private_directory(path: Path) -> None:
    try:
        if os.name == "nt":  # pragma: no cover - exercised by Windows CI
            mutation_lock._prepare_windows_private_directory(path)
        else:
            existed = path.exists()
            path.mkdir(parents=True, mode=0o700, exist_ok=True)
            if not existed:
                os.chmod(path, 0o700)
        if not _private_parent_is_safe(path):
            raise AuthorizationCustodyUnavailable
    except AuthorizationCustodyUnavailable:
        raise
    except (OSError, RuntimeError, ValueError):
        raise AuthorizationCustodyUnavailable from None


def _prepare_host_registry_parent(path: Path) -> None:
    root = _standalone_host_control_root()
    if path.parent.parent != root:
        raise AuthorizationCustodyUnavailable
    _prepare_private_directory(root)
    _prepare_private_directory(path.parent)


def _host_control_key_path() -> Path:
    return _standalone_host_control_root() / _HOST_CONTROL_KEY_NAME


def _host_control_key_id(host_key: bytes) -> str:
    if not isinstance(host_key, bytes) or len(host_key) != _HOST_CONTROL_KEY_BYTES:
        raise AuthorizationCustodyUnavailable
    return f"standalone-host-key-v1-{hashlib.sha256(host_key).hexdigest()}"


def _load_host_control_key() -> bytes:
    loaded = _load_private_artifact(
        _host_control_key_path(),
        maximum_bytes=_HOST_CONTROL_KEY_BYTES,
    )
    if len(loaded.data) != _HOST_CONTROL_KEY_BYTES:
        raise AuthorizationCustodyUnavailable
    return loaded.data


def _ensure_host_control_key() -> bytes:
    root = _standalone_host_control_root()
    _prepare_private_directory(root)
    path = _host_control_key_path()
    try:
        return _load_host_control_key()
    except AuthorizationCustodyUnavailable:
        if path.exists():
            raise
    candidate = secrets.token_bytes(_HOST_CONTROL_KEY_BYTES)
    _publish_private_artifact(
        path,
        candidate,
        maximum_bytes=_HOST_CONTROL_KEY_BYTES,
    )
    return _load_host_control_key()


def _host_registry_record(
    control: AuthorizationControlRecord,
    *,
    state: str,
    no_in_flight: bool,
    updated_at: int,
    host_key_id: str,
) -> _StandaloneHostRegistryRecord:
    if (
        _STANDALONE_ATTACHMENT.fullmatch(control.registry_attachment_id) is None
        or state not in {"SERVING", "DRAINING"}
        or not isinstance(no_in_flight, bool)
        or (state == "SERVING" and no_in_flight)
    ):
        raise AuthorizationCustodyUnavailable
    return _StandaloneHostRegistryRecord(
        version=1,
        cell_id=control.cell_id,
        logical_vault_id=control.logical_vault_id,
        keyring_id=control.keyring_id,
        registry_attachment_id=control.registry_attachment_id,
        attachment_epoch=control.attachment_epoch,
        serving_membership_epoch=control.serving_membership_epoch,
        serving_membership_digest=control.serving_membership_digest,
        state=state,
        no_in_flight=no_in_flight,
        updated_at=_bounded_time(updated_at),
        signing_key_id=_bounded_identifier(host_key_id),
    )


def _host_registry_matches_control(
    record: _StandaloneHostRegistryRecord,
    control: AuthorizationControlRecord,
    *,
    state: str | None = None,
    no_in_flight: bool | None = None,
) -> bool:
    return (
        record.cell_id == control.cell_id
        and record.logical_vault_id == control.logical_vault_id
        and record.keyring_id == control.keyring_id
        and record.registry_attachment_id == control.registry_attachment_id
        and record.attachment_epoch == control.attachment_epoch
        and record.serving_membership_epoch == control.serving_membership_epoch
        and record.serving_membership_digest == control.serving_membership_digest
        and (state is None or record.state == state)
        and (no_in_flight is None or record.no_in_flight is no_in_flight)
    )


def _load_host_registry(
    control: AuthorizationControlRecord,
    *,
    now: int,
) -> tuple[Path, bytes, _StandaloneHostRegistryRecord, bytes]:
    path = _host_registry_path(control.logical_vault_id)
    loaded = _load_file(path)
    host_key = _load_host_control_key()
    return (
        path,
        loaded.data,
        _parse_host_registry(loaded.data, host_key=host_key, now=now),
        host_key,
    )


def require_current_standalone_registry(
    custody: AuthorizationCustody,
    *,
    now: int,
    require_serving: bool,
) -> None:
    """Recheck the non-portable host attachment before session authority commits."""

    if not isinstance(custody, AuthorizationCustody):
        raise AuthorizationCustodyUnavailable
    control = custody.control
    if not control.registry_attachment_id.startswith("attachment-v1-"):
        return
    _path, _raw, record, _host_key = _load_host_registry(control, now=now)
    if not _host_registry_matches_control(record, control) or (
        require_serving and record.state != "SERVING"
    ):
        raise AuthorizationCustodyUnavailable


def require_standalone_mutation_admission(
    vault_root: Path,
    *,
    now: int,
) -> None:
    """Block ordinary mutations while the host attachment is draining."""

    root = Path(vault_root)
    if not root.is_absolute():
        return
    configured = (
        os.environ.get(KEYRING_FILE_ENV, "").strip(),
        os.environ.get(CONTROL_FILE_ENV, "").strip(),
    )
    if configured == ("", ""):
        return
    if not all(configured):
        raise AuthorizationCustodyUnavailable
    custody = load_authorization_custody(root, now=now)
    require_current_standalone_registry(
        custody,
        now=now,
        require_serving=True,
    )


def _publish_initial_host_registry(
    control: AuthorizationControlRecord,
    *,
    now: int,
    state: str = "SERVING",
    no_in_flight: bool = False,
) -> None:
    path = _host_registry_path(control.logical_vault_id)
    _prepare_host_registry_parent(path)
    host_key = _ensure_host_control_key()
    try:
        _existing_path, _raw, existing, _existing_host_key = _load_host_registry(
            control,
            now=now,
        )
    except AuthorizationCustodyUnavailable:
        if path.exists():
            raise
    else:
        if not _host_registry_matches_control(
            existing,
            control,
            state=state,
            no_in_flight=no_in_flight,
        ):
            raise AuthorizationCustodyUnavailable
        return
    record = _host_registry_record(
        control,
        state=state,
        no_in_flight=no_in_flight,
        updated_at=now,
        host_key_id=_host_control_key_id(host_key),
    )
    encoded = _signed_host_registry_bytes(record, signing_key=host_key)
    _publish_private_file(path, encoded)


def _advance_host_registry(
    *,
    source_control: AuthorizationControlRecord,
    source_state: str,
    source_no_in_flight: bool,
    target_control: AuthorizationControlRecord,
    target_state: str,
    target_no_in_flight: bool,
    now: int,
) -> None:
    path, raw, current, host_key = _load_host_registry(source_control, now=now)
    if _host_registry_matches_control(
        current,
        target_control,
        state=target_state,
        no_in_flight=target_no_in_flight,
    ):
        return
    if not _host_registry_matches_control(
        current,
        source_control,
        state=source_state,
        no_in_flight=source_no_in_flight,
    ):
        raise AuthorizationCustodyUnavailable
    target = _host_registry_record(
        target_control,
        state=target_state,
        no_in_flight=target_no_in_flight,
        updated_at=now,
        host_key_id=_host_control_key_id(host_key),
    )
    _replace_control_bytes(
        path,
        expected=raw,
        target=_signed_host_registry_bytes(target, signing_key=host_key),
    )


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

    return _publish_private_artifact(
        path,
        data,
        maximum_bytes=MAX_CUSTODY_FILE_BYTES,
    )


def _publish_private_artifact(
    path: Path,
    data: bytes,
    *,
    maximum_bytes: int,
) -> bytes:
    """Publish one immutable bounded private artifact, or adopt an exact retry."""

    if (
        maximum_bytes < 1
        or not isinstance(data, bytes)
        or not 1 <= len(data) <= maximum_bytes
    ):
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
                    installed = _load_private_artifact(
                        path,
                        maximum_bytes=maximum_bytes,
                    )
                    if not hmac.compare_digest(installed.data, data):
                        raise AuthorizationCustodyUnavailable
                    return installed.data
                flushed = filesystem.flush_directory(parent)
                if not flushed.ok:
                    raise AuthorizationCustodyUnavailable
        installed = _load_private_artifact(
            path,
            maximum_bytes=maximum_bytes,
        )
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
    state: str = "SERVING",
    schema_version: int = 4,
    issuance_stopped: bool = False,
    no_in_flight: bool = False,
    previous_epoch_digest: str | None = None,
    attested_at: int | None = None,
) -> bytes:
    """Build the authenticated singleton form of the fleet record."""

    if control.serving_membership_epoch < 1:
        raise AuthorizationCustodyUnavailable
    moment = control.issued_at if attested_at is None else _bounded_time(attested_at)
    accepted_key_ids = tuple(sorted(key.key_id for key in keyring.accepted_keys))
    attestation = authorization_serving_membership.ReplicaReadinessAttestation(
        version=1,
        epoch=control.serving_membership_epoch,
        replica_id=_bounded_identifier(replica_id),
        state=state,
        software_version=runtime_software_version(),
        schema_version=schema_version,
        cell_id=control.cell_id,
        active_key_id=keyring.active_key_id,
        accepted_key_ids=accepted_key_ids,
        control_digest=control_attestation_digest(control),
        keyring_digest=keyring_attestation_digest(keyring),
        attested_at=moment,
        expires_at=min(
            control.expires_at,
            moment
            + authorization_serving_membership.MAX_ATTESTATION_TTL_SECONDS,
        ),
        issuance_stopped=issuance_stopped,
        no_in_flight=no_in_flight,
        signing_key_id=keyring.active_key_id,
    )
    record = authorization_serving_membership.ServingMembershipEpoch(
        version=1,
        epoch=control.serving_membership_epoch,
        cell_id=control.cell_id,
        logical_vault_id=control.logical_vault_id,
        previous_epoch_digest=previous_epoch_digest,
        issued_at=moment,
        expires_at=min(
            control.expires_at,
            moment
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
        staged_at=keyring.active_key.not_before,
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
                state="DRAINING",
                schema_version=3,
                issuance_stopped=True,
                no_in_flight=True,
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
                state="DRAINING",
                schema_version=3,
                issuance_stopped=True,
                no_in_flight=True,
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
        _publish_initial_host_registry(
            control,
            now=current_time,
            state="DRAINING",
            no_in_flight=True,
        )

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


def _migration_membership_barrier(point: str) -> None:
    """Test seam between the two fail-closed external publication effects."""

    del point


def _require_singleton_migration_membership(
    record: ServingMembershipEpoch,
    *,
    keyring: AuthorizationKeyring,
    control: AuthorizationControlRecord,
    replica_id: str,
    state: str,
    schema_version: int,
    issuance_stopped: bool,
    no_in_flight: bool,
) -> None:
    expected_keys = tuple(sorted(item.key_id for item in keyring.accepted_keys))
    if (
        len(record.replicas) != 1
        or record.cell_id != control.cell_id
        or record.logical_vault_id != control.logical_vault_id
    ):
        raise AuthorizationCustodyUnavailable
    replica = record.replicas[0]
    if (
        replica.replica_id != replica_id
        or replica.state != state
        or replica.schema_version != schema_version
        or replica.issuance_stopped is not issuance_stopped
        or replica.no_in_flight is not no_in_flight
        or replica.active_key_id != keyring.active_key_id
        or replica.accepted_key_ids != expected_keys
        or replica.control_digest != control_attestation_digest(control)
        or replica.keyring_digest != keyring_attestation_digest(keyring)
    ):
        raise AuthorizationCustodyUnavailable


def _parse_migration_membership(
    raw: bytes,
    *,
    keyring: AuthorizationKeyring,
    control: AuthorizationControlRecord,
    now: int,
    epoch: int,
    digest: str,
) -> ServingMembershipEpoch:
    try:
        return authorization_serving_membership.parse_serving_membership(
            raw,
            verifier_keys={item.key_id: item.key for item in keyring.accepted_keys},
            now=now,
            expected_cell_id=control.cell_id,
            expected_logical_vault_id=control.logical_vault_id,
            expected_epoch=epoch,
            expected_digest=digest,
        )
    except authorization_serving_membership.ServingMembershipUnavailable:
        raise AuthorizationCustodyUnavailable from None


def complete_standalone_v4_migration(
    vault_root: Path,
    *,
    target: VerifiedActiveGovernanceState,
    now: int,
) -> AuthorizationCustody:
    """Advance one drained standalone v3 membership to exact serving v4.

    The membership successor is published before the signed control record.  The
    interval between those two files is intentionally unavailable to loaders;
    an exact retry recognizes the signed successor through its predecessor digest
    and completes only the missing control compare-and-swap.
    """

    from . import schema_v4

    if not isinstance(target, schema_v4.VerifiedActiveGovernanceState):
        raise AuthorizationCustodyUnavailable
    current_time = _bounded_time(now)
    root = Path(vault_root)
    external = load_external_custody(root)
    keyring = parse_keyring(external.keyring)
    control = parse_control_record(external.control, keyring=keyring, now=current_time)
    _verify_registered_attachment(root, control.registry_attachment_id)
    replica_id = _bounded_identifier(os.environ.get(REPLICA_ID_ENV, ""))
    membership_path = _configured_external_path(MEMBERSHIP_FILE_ENV, root)
    if membership_path in {external.keyring_path, external.control_path}:
        raise AuthorizationCustodyUnavailable
    membership_raw = _load_file(membership_path).data
    if (
        not control.governance_enrolled
        or control.logical_vault_id != target.logical_vault_id
        or control.activation_store_id != target.activation_store_id
        or control.activation_epoch != target.activation_epoch
        or control.activation_state_digest != target.activation_state_digest
    ):
        raise AuthorizationCustodyUnavailable

    target_control: AuthorizationControlRecord
    if control.serving_membership_epoch == 1:
        try:
            predecessor = _parse_migration_membership(
                membership_raw,
                keyring=keyring,
                control=control,
                now=current_time,
                epoch=1,
                digest=control.serving_membership_digest,
            )
        except AuthorizationCustodyUnavailable:
            successor_digest = authorization_serving_membership.serving_membership_digest(
                membership_raw
            )
            successor = _parse_migration_membership(
                membership_raw,
                keyring=keyring,
                control=control,
                now=current_time,
                epoch=2,
                digest=successor_digest,
            )
            if successor.previous_epoch_digest != control.serving_membership_digest:
                raise AuthorizationCustodyUnavailable from None
            _require_singleton_migration_membership(
                successor,
                keyring=keyring,
                control=control,
                replica_id=replica_id,
                state="SERVING",
                schema_version=4,
                issuance_stopped=False,
                no_in_flight=False,
            )
            target_membership = membership_raw
        else:
            if predecessor.previous_epoch_digest is not None:
                raise AuthorizationCustodyUnavailable
            _require_singleton_migration_membership(
                predecessor,
                keyring=keyring,
                control=control,
                replica_id=replica_id,
                state="DRAINING",
                schema_version=3,
                issuance_stopped=True,
                no_in_flight=True,
            )
            provisional = replace(
                control,
                serving_membership_epoch=2,
                serving_membership_digest="0" * 64,
            )
            target_membership = _standalone_membership_bytes(
                keyring=keyring,
                control=provisional,
                replica_id=replica_id,
                previous_epoch_digest=predecessor.record_digest,
                attested_at=current_time,
            )
            successor = _parse_migration_membership(
                target_membership,
                keyring=keyring,
                control=provisional,
                now=current_time,
                epoch=2,
                digest=(
                    authorization_serving_membership.serving_membership_digest(
                        target_membership
                    )
                ),
            )
            try:
                authorization_serving_membership.validate_membership_successor(
                    predecessor,
                    successor,
                    now=current_time,
                )
            except authorization_serving_membership.ServingMembershipUnavailable:
                raise AuthorizationCustodyUnavailable from None
            _replace_control_bytes(
                membership_path,
                expected=membership_raw,
                target=target_membership,
            )
        target_control = replace(
            control,
            serving_membership_epoch=2,
            serving_membership_digest=(
                authorization_serving_membership.serving_membership_digest(
                    target_membership
                )
            ),
        )
        _migration_membership_barrier("after_membership_publish")
        signing_key = next(
            (
                item.key
                for item in keyring.accepted_keys
                if item.key_id == target_control.signing_key_id
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
    elif control.serving_membership_epoch == 2:
        successor = _parse_migration_membership(
            membership_raw,
            keyring=keyring,
            control=control,
            now=current_time,
            epoch=2,
            digest=control.serving_membership_digest,
        )
        if successor.previous_epoch_digest is None:
            raise AuthorizationCustodyUnavailable
        _require_singleton_migration_membership(
            successor,
            keyring=keyring,
            control=control,
            replica_id=replica_id,
            state="SERVING",
            schema_version=4,
            issuance_stopped=False,
            no_in_flight=False,
        )
        target_control = control
    else:
        raise AuthorizationCustodyUnavailable

    if successor.previous_epoch_digest is None:
        raise AuthorizationCustodyUnavailable
    _advance_host_registry(
        source_control=replace(
            target_control,
            serving_membership_epoch=1,
            serving_membership_digest=successor.previous_epoch_digest,
        ),
        source_state="DRAINING",
        source_no_in_flight=True,
        target_control=target_control,
        target_state="SERVING",
        target_no_in_flight=False,
        now=current_time,
    )
    verified = load_authorization_custody(root, now=current_time)
    if (
        verified.control != target_control
        or verified.serving_membership is None
        or verified.serving_membership.epoch != 2
        or verified.local_replica_id != replica_id
    ):
        raise AuthorizationCustodyUnavailable
    return verified


def _standalone_membership_file(
    vault_root: Path,
    *,
    external: ExternalAuthorizationCustody,
) -> tuple[Path, bytes, str]:
    membership_path = _configured_external_path(MEMBERSHIP_FILE_ENV, vault_root)
    replica_id = _bounded_identifier(os.environ.get(REPLICA_ID_ENV, ""))
    if membership_path in {external.keyring_path, external.control_path}:
        raise AuthorizationCustodyUnavailable
    return membership_path, _load_file(membership_path).data, replica_id


def _require_standalone_membership(
    record: ServingMembershipEpoch,
    *,
    keyring: AuthorizationKeyring,
    control: AuthorizationControlRecord,
    replica_id: str,
    state: str,
    no_in_flight: bool,
) -> None:
    if (
        len(record.replicas) != 1
        or record.cell_id != control.cell_id
        or record.logical_vault_id != control.logical_vault_id
    ):
        raise AuthorizationCustodyUnavailable
    replica = record.replicas[0]
    expected_keys = tuple(sorted(item.key_id for item in keyring.accepted_keys))
    if (
        replica.replica_id != replica_id
        or replica.state != state
        or replica.software_version != runtime_software_version()
        or replica.schema_version != 4
        or replica.issuance_stopped is not (state == "DRAINING")
        or replica.no_in_flight is not no_in_flight
        or replica.active_key_id != keyring.active_key_id
        or replica.accepted_key_ids != expected_keys
        or replica.control_digest != control_attestation_digest(control)
        or replica.keyring_digest != keyring_attestation_digest(keyring)
    ):
        raise AuthorizationCustodyUnavailable


def _standalone_membership_successor(
    raw: bytes,
    *,
    keyring: AuthorizationKeyring,
    expected_control: AuthorizationControlRecord,
    target_control: AuthorizationControlRecord,
    replica_id: str,
    target_state: str,
    target_no_in_flight: bool,
    now: int,
) -> ServingMembershipEpoch:
    digest = authorization_serving_membership.serving_membership_digest(raw)
    successor = _parse_migration_membership(
        raw,
        keyring=keyring,
        control=target_control,
        now=now,
        epoch=expected_control.serving_membership_epoch + 1,
        digest=digest,
    )
    if successor.previous_epoch_digest != expected_control.serving_membership_digest:
        raise AuthorizationCustodyUnavailable
    _require_standalone_membership(
        successor,
        keyring=keyring,
        control=target_control,
        replica_id=replica_id,
        state=target_state,
        no_in_flight=target_no_in_flight,
    )
    return successor


def _historical_membership_verification_time(raw: bytes, *, now: int) -> int:
    """Choose a bounded time for authenticating an exact expired predecessor."""

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_closed_object)
        if not isinstance(value, dict):
            raise AuthorizationCustodyUnavailable
        issued_at = _bounded_time(value["issued_at"])
        expires_at = _bounded_time(value["expires_at"])
        if issued_at >= expires_at:
            raise AuthorizationCustodyUnavailable
        return min(_bounded_time(now), expires_at - 1)
    except AuthorizationCustodyUnavailable:
        raise
    except (KeyError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        raise AuthorizationCustodyUnavailable from None


def _standalone_transition_replay_control(
    current: AuthorizationControlRecord,
    *,
    expected: AuthorizationControlRecord,
) -> bool:
    return (
        current.serving_membership_epoch == expected.serving_membership_epoch + 1
        and replace(
            current,
            serving_membership_epoch=expected.serving_membership_epoch,
            serving_membership_digest=expected.serving_membership_digest,
        )
        == expected
    )


def _standalone_transition_descendant_control(
    current: AuthorizationControlRecord,
    *,
    expected: AuthorizationControlRecord,
) -> bool:
    return (
        current.serving_membership_epoch > expected.serving_membership_epoch
        and replace(
            current,
            serving_membership_epoch=expected.serving_membership_epoch,
            serving_membership_digest=expected.serving_membership_digest,
        )
        == expected
    )


def _standalone_transition_barrier(point: str) -> None:
    """Crash-injection seam between standalone membership publications."""

    del point


def _transition_standalone_attachment_membership(
    vault_root: Path,
    *,
    expected_control: AuthorizationControlRecord,
    source_state: str,
    source_no_in_flight: bool,
    target_state: str,
    target_no_in_flight: bool,
    now: int,
    recover_expired_source: bool = False,
) -> AuthorizationCustody:
    if not isinstance(expected_control, AuthorizationControlRecord):
        raise AuthorizationCustodyUnavailable
    root = Path(vault_root)
    current_time = _bounded_time(now)

    from .. import reserved_paths

    with reserved_paths._identity_coordination_scope(root):
        external = load_external_custody(root)
        keyring = parse_keyring(external.keyring)
        current_control = parse_control_record(
            external.control,
            keyring=keyring,
            now=current_time,
        )
        _verify_registered_attachment(root, current_control.registry_attachment_id)
        membership_path, membership_raw, replica_id = _standalone_membership_file(
            root,
            external=external,
        )

        registry_source_control = expected_control
        registry_source_state = source_state
        registry_source_no_in_flight = source_no_in_flight
        if current_control != expected_control:
            replay_is_valid = _standalone_transition_replay_control(
                current_control,
                expected=expected_control,
            )
            if recover_expired_source:
                replay_is_valid = _standalone_transition_descendant_control(
                    current_control,
                    expected=expected_control,
                )
            if not replay_is_valid:
                raise AuthorizationCustodyUnavailable
            verification_time = (
                _historical_membership_verification_time(
                    membership_raw,
                    now=current_time,
                )
                if recover_expired_source
                else current_time
            )
            try:
                current_record = _parse_migration_membership(
                    membership_raw,
                    keyring=keyring,
                    control=current_control,
                    now=verification_time,
                    epoch=current_control.serving_membership_epoch,
                    digest=current_control.serving_membership_digest,
                )
            except AuthorizationCustodyUnavailable:
                if not recover_expired_source:
                    raise
                provisional_target = replace(
                    current_control,
                    serving_membership_epoch=(
                        current_control.serving_membership_epoch + 1
                    ),
                    serving_membership_digest="0" * 64,
                )
                successor = _standalone_membership_successor(
                    membership_raw,
                    keyring=keyring,
                    expected_control=current_control,
                    target_control=provisional_target,
                    replica_id=replica_id,
                    target_state=target_state,
                    target_no_in_flight=target_no_in_flight,
                    now=verification_time,
                )
                if successor.expires_at <= current_time:
                    target_membership = _standalone_membership_bytes(
                        keyring=keyring,
                        control=provisional_target,
                        replica_id=replica_id,
                        state=target_state,
                        issuance_stopped=target_state == "DRAINING",
                        no_in_flight=target_no_in_flight,
                        previous_epoch_digest=(
                            current_control.serving_membership_digest
                        ),
                        attested_at=current_time,
                    )
                    successor = _standalone_membership_successor(
                        target_membership,
                        keyring=keyring,
                        expected_control=current_control,
                        target_control=provisional_target,
                        replica_id=replica_id,
                        target_state=target_state,
                        target_no_in_flight=target_no_in_flight,
                        now=current_time,
                    )
                    _replace_control_bytes(
                        membership_path,
                        expected=membership_raw,
                        target=target_membership,
                    )
                    _standalone_transition_barrier("after-membership")
                if (
                    current_control.serving_membership_epoch
                    == expected_control.serving_membership_epoch + 1
                ):
                    _advance_host_registry(
                        source_control=expected_control,
                        source_state=source_state,
                        source_no_in_flight=source_no_in_flight,
                        target_control=current_control,
                        target_state=target_state,
                        target_no_in_flight=target_no_in_flight,
                        now=current_time,
                    )
                else:
                    _path, _raw, registry, _host_key = _load_host_registry(
                        current_control,
                        now=current_time,
                    )
                    if not _host_registry_matches_control(
                        registry,
                        current_control,
                        state=target_state,
                        no_in_flight=target_no_in_flight,
                    ):
                        raise AuthorizationCustodyUnavailable from None
                target_control = replace(
                    provisional_target,
                    serving_membership_digest=successor.record_digest,
                )
                signing_key = next(
                    (
                        item.key
                        for item in keyring.accepted_keys
                        if item.key_id == target_control.signing_key_id
                    ),
                    None,
                )
                if signing_key is None:
                    raise AuthorizationCustodyUnavailable from None
                _replace_control_bytes(
                    external.control_path,
                    expected=external.control,
                    target=_signed_control_bytes(
                        target_control,
                        signing_key=signing_key,
                    ),
                )
                _standalone_transition_barrier("after-control")
                registry_source_control = current_control
                registry_source_state = target_state
                registry_source_no_in_flight = target_no_in_flight
            else:
                _require_standalone_membership(
                    current_record,
                    keyring=keyring,
                    control=current_control,
                    replica_id=replica_id,
                    state=target_state,
                    no_in_flight=target_no_in_flight,
                )
                if (
                    current_control.serving_membership_epoch
                    == expected_control.serving_membership_epoch + 1
                ):
                    predecessor_control = expected_control
                    predecessor_state = source_state
                    predecessor_no_in_flight = source_no_in_flight
                else:
                    if current_record.previous_epoch_digest is None:
                        raise AuthorizationCustodyUnavailable
                    predecessor_control = replace(
                        current_control,
                        serving_membership_epoch=(
                            current_control.serving_membership_epoch - 1
                        ),
                        serving_membership_digest=(
                            current_record.previous_epoch_digest
                        ),
                    )
                    predecessor_state = target_state
                    predecessor_no_in_flight = target_no_in_flight
                _advance_host_registry(
                    source_control=predecessor_control,
                    source_state=predecessor_state,
                    source_no_in_flight=predecessor_no_in_flight,
                    target_control=current_control,
                    target_state=target_state,
                    target_no_in_flight=target_no_in_flight,
                    now=current_time,
                )
                if recover_expired_source and current_record.expires_at <= current_time:
                    provisional_target = replace(
                        current_control,
                        serving_membership_epoch=(
                            current_control.serving_membership_epoch + 1
                        ),
                        serving_membership_digest="0" * 64,
                    )
                    target_membership = _standalone_membership_bytes(
                        keyring=keyring,
                        control=provisional_target,
                        replica_id=replica_id,
                        state=target_state,
                        issuance_stopped=target_state == "DRAINING",
                        no_in_flight=target_no_in_flight,
                        previous_epoch_digest=current_record.record_digest,
                        attested_at=current_time,
                    )
                    successor = _standalone_membership_successor(
                        target_membership,
                        keyring=keyring,
                        expected_control=current_control,
                        target_control=provisional_target,
                        replica_id=replica_id,
                        target_state=target_state,
                        target_no_in_flight=target_no_in_flight,
                        now=current_time,
                    )
                    try:
                        authorization_serving_membership.validate_membership_successor(
                            current_record,
                            successor,
                            now=current_time,
                        )
                    except authorization_serving_membership.ServingMembershipUnavailable:
                        raise AuthorizationCustodyUnavailable from None
                    _replace_control_bytes(
                        membership_path,
                        expected=membership_raw,
                        target=target_membership,
                    )
                    _standalone_transition_barrier("after-membership")
                    target_control = replace(
                        provisional_target,
                        serving_membership_digest=successor.record_digest,
                    )
                    signing_key = next(
                        (
                            item.key
                            for item in keyring.accepted_keys
                            if item.key_id == target_control.signing_key_id
                        ),
                        None,
                    )
                    if signing_key is None:
                        raise AuthorizationCustodyUnavailable
                    _replace_control_bytes(
                        external.control_path,
                        expected=external.control,
                        target=_signed_control_bytes(
                            target_control,
                            signing_key=signing_key,
                        ),
                    )
                    _standalone_transition_barrier("after-control")
                    registry_source_control = current_control
                    registry_source_state = target_state
                    registry_source_no_in_flight = target_no_in_flight
                else:
                    target_control = current_control
                    registry_source_control = current_control
                    registry_source_state = target_state
                    registry_source_no_in_flight = target_no_in_flight
        else:
            source_record: ServingMembershipEpoch | None
            source_verification_time = (
                _historical_membership_verification_time(
                    membership_raw,
                    now=current_time,
                )
                if recover_expired_source
                else current_time
            )
            try:
                source_record = _parse_migration_membership(
                    membership_raw,
                    keyring=keyring,
                    control=current_control,
                    now=source_verification_time,
                    epoch=current_control.serving_membership_epoch,
                    digest=current_control.serving_membership_digest,
                )
            except AuthorizationCustodyUnavailable:
                source_record = None

            provisional_target = replace(
                current_control,
                serving_membership_epoch=current_control.serving_membership_epoch + 1,
                serving_membership_digest="0" * 64,
            )
            if source_record is None:
                successor = _standalone_membership_successor(
                    membership_raw,
                    keyring=keyring,
                    expected_control=current_control,
                    target_control=provisional_target,
                    replica_id=replica_id,
                    target_state=target_state,
                    target_no_in_flight=target_no_in_flight,
                    now=(
                        _historical_membership_verification_time(
                            membership_raw,
                            now=current_time,
                        )
                        if recover_expired_source
                        else current_time
                    ),
                )
                if successor.expires_at <= current_time:
                    target_membership = _standalone_membership_bytes(
                        keyring=keyring,
                        control=provisional_target,
                        replica_id=replica_id,
                        state=target_state,
                        issuance_stopped=target_state == "DRAINING",
                        no_in_flight=target_no_in_flight,
                        previous_epoch_digest=(
                            current_control.serving_membership_digest
                        ),
                        attested_at=current_time,
                    )
                    successor = _standalone_membership_successor(
                        target_membership,
                        keyring=keyring,
                        expected_control=current_control,
                        target_control=provisional_target,
                        replica_id=replica_id,
                        target_state=target_state,
                        target_no_in_flight=target_no_in_flight,
                        now=current_time,
                    )
                    _replace_control_bytes(
                        membership_path,
                        expected=membership_raw,
                        target=target_membership,
                    )
                    _standalone_transition_barrier("after-membership")
                else:
                    target_membership = membership_raw
            else:
                _require_standalone_membership(
                    source_record,
                    keyring=keyring,
                    control=current_control,
                    replica_id=replica_id,
                    state=source_state,
                    no_in_flight=source_no_in_flight,
                )
                target_membership = _standalone_membership_bytes(
                    keyring=keyring,
                    control=provisional_target,
                    replica_id=replica_id,
                    state=target_state,
                    issuance_stopped=target_state == "DRAINING",
                    no_in_flight=target_no_in_flight,
                    previous_epoch_digest=source_record.record_digest,
                    attested_at=current_time,
                )
                successor = _standalone_membership_successor(
                    target_membership,
                    keyring=keyring,
                    expected_control=current_control,
                    target_control=provisional_target,
                    replica_id=replica_id,
                    target_state=target_state,
                    target_no_in_flight=target_no_in_flight,
                    now=current_time,
                )
                try:
                    authorization_serving_membership.validate_membership_successor(
                        source_record,
                        successor,
                        now=current_time,
                    )
                except authorization_serving_membership.ServingMembershipUnavailable:
                    raise AuthorizationCustodyUnavailable from None
                _replace_control_bytes(
                    membership_path,
                    expected=membership_raw,
                    target=target_membership,
                )
                _standalone_transition_barrier("after-membership")

            target_control = replace(
                provisional_target,
                serving_membership_digest=successor.record_digest,
            )
            signing_key = next(
                (
                    item.key
                    for item in keyring.accepted_keys
                    if item.key_id == target_control.signing_key_id
                ),
                None,
            )
            if signing_key is None:
                raise AuthorizationCustodyUnavailable
            _replace_control_bytes(
                external.control_path,
                expected=external.control,
                target=_signed_control_bytes(
                    target_control,
                    signing_key=signing_key,
                ),
            )
            _standalone_transition_barrier("after-control")
        _advance_host_registry(
            source_control=registry_source_control,
            source_state=registry_source_state,
            source_no_in_flight=registry_source_no_in_flight,
            target_control=target_control,
            target_state=target_state,
            target_no_in_flight=target_no_in_flight,
            now=current_time,
        )
        _standalone_transition_barrier("after-registry")

    verified = load_authorization_custody(root, now=current_time)
    if verified.serving_membership is None:
        raise AuthorizationCustodyUnavailable
    _require_standalone_membership(
        verified.serving_membership,
        keyring=verified.keyring,
        control=verified.control,
        replica_id=_bounded_identifier(os.environ.get(REPLICA_ID_ENV, "")),
        state=target_state,
        no_in_flight=target_no_in_flight,
    )
    return verified


def begin_standalone_attachment_drain(
    vault_root: Path,
    *,
    expected_control: AuthorizationControlRecord,
    now: int,
) -> AuthorizationCustody:
    """Stop standalone session issuance before an attachment can detach."""

    root = Path(vault_root)
    from .. import writer_lease

    with writer_lease.get_manager().mutation_guard(
        root,
        operation="authorization-attachment-drain",
        holder_kind="authorization-attachment-control",
        attachment_control=True,
        attachment_now=now,
    ):
        return _transition_standalone_attachment_membership(
            root,
            expected_control=expected_control,
            source_state="SERVING",
            source_no_in_flight=False,
            target_state="DRAINING",
            target_no_in_flight=False,
            now=now,
        )


def acknowledge_standalone_attachment_drain(
    vault_root: Path,
    *,
    expected_control: AuthorizationControlRecord,
    now: int,
) -> AuthorizationCustody:
    """Record the explicit no-in-flight acknowledgement for one drained root."""

    root = Path(vault_root)
    from .. import writer_lease

    with writer_lease.get_manager().mutation_guard(
        root,
        operation="authorization-attachment-drain-acknowledgement",
        holder_kind="authorization-attachment-control",
        attachment_control=True,
        attachment_now=now,
    ):
        return _transition_standalone_attachment_membership(
            root,
            expected_control=expected_control,
            source_state="DRAINING",
            source_no_in_flight=False,
            target_state="DRAINING",
            target_no_in_flight=True,
            now=now,
        )


def prepare_standalone_attachment_transfer(
    source_vault_root: Path,
    target_vault_root: Path,
    *,
    expected_control: AuthorizationControlRecord,
    now: int,
) -> bytes:
    """Mint a short-lived target-bound detach acknowledgement after drain."""

    source = Path(source_vault_root)
    current_time = _bounded_time(now)
    custody = load_authorization_custody(source, now=current_time)
    if custody.control != expected_control or custody.serving_membership is None:
        raise AuthorizationCustodyUnavailable
    replica_id = _bounded_identifier(os.environ.get(REPLICA_ID_ENV, ""))
    _require_standalone_membership(
        custody.serving_membership,
        keyring=custody.keyring,
        control=custody.control,
        replica_id=replica_id,
        state="DRAINING",
        no_in_flight=True,
    )
    target_attachment = standalone_attachment_id(Path(target_vault_root))
    if hmac.compare_digest(
        custody.control.registry_attachment_id,
        target_attachment,
    ):
        raise AuthorizationCustodyUnavailable
    external = load_external_custody(source)
    signing_key = next(
        (
            item
            for item in custody.keyring.accepted_keys
            if item.key_id == custody.control.signing_key_id
        ),
        None,
    )
    if signing_key is None:
        raise AuthorizationCustodyUnavailable
    acknowledgement = _StandaloneDetachAcknowledgement(
        version=1,
        cell_id=custody.control.cell_id,
        logical_vault_id=custody.control.logical_vault_id,
        keyring_id=custody.control.keyring_id,
        source_registry_attachment_id=custody.control.registry_attachment_id,
        target_registry_attachment_id=target_attachment,
        source_attachment_epoch=custody.control.attachment_epoch,
        target_attachment_epoch=custody.control.attachment_epoch + 1,
        source_membership_epoch=custody.control.serving_membership_epoch,
        source_membership_digest=custody.control.serving_membership_digest,
        source_control_digest=hashlib.sha256(external.control).hexdigest(),
        issued_at=current_time,
        expires_at=min(
            custody.control.expires_at,
            current_time
            + authorization_serving_membership.MAX_ATTESTATION_TTL_SECONDS,
        ),
        signing_key_id=custody.control.signing_key_id,
    )
    return _signed_detach_ack_bytes(
        acknowledgement,
        signing_key=signing_key.key,
    )


def _require_attachment_target_authority(
    vault_root: Path,
    *,
    control: AuthorizationControlRecord,
    now: int,
    invalidate_sessions: bool,
) -> None:
    if not control.governance_enrolled:
        _governance_negative_scan(vault_root)
        return
    if (
        control.activation_store_id is None
        or control.activation_epoch is None
        or control.activation_state_digest is None
    ):
        raise AuthorizationCustodyUnavailable
    from . import schema_v4, store

    try:
        connection = (
            store.open_authorization_session_connection(vault_root)
            if invalidate_sessions
            else store.open_active_governance_read_connection(vault_root)
        )
    except (store.UnsupportedGovernanceSchema, OSError, RuntimeError):
        raise AuthorizationCustodyUnavailable from None
    try:
        active = schema_v4.load_active_state(
            connection,
            expected_logical_vault_id=control.logical_vault_id,
            expected_activation_store_id=control.activation_store_id,
            expected_activation_epoch=control.activation_epoch,
            expected_activation_state_digest=control.activation_state_digest,
        )
        if (
            active.logical_vault_id != control.logical_vault_id
            or active.activation_store_id != control.activation_store_id
            or active.activation_epoch != control.activation_epoch
            or active.activation_state_digest != control.activation_state_digest
        ):
            raise AuthorizationCustodyUnavailable
        if invalidate_sessions:
            schema_v4.invalidate_attachment_session_authority(
                connection,
                invalidated_at=now,
            )
            schema_v4.load_active_state(
                connection,
                expected_logical_vault_id=control.logical_vault_id,
                expected_activation_store_id=control.activation_store_id,
                expected_activation_epoch=control.activation_epoch,
                expected_activation_state_digest=control.activation_state_digest,
            )
    except (schema_v4.SchemaV4Error, OSError, RuntimeError, TypeError, ValueError):
        raise AuthorizationCustodyUnavailable from None
    finally:
        connection.close()


def _complete_standalone_attachment_transfer(
    target_vault_root: Path,
    *,
    acknowledgement: bytes,
    target_state: str,
    target_no_in_flight: bool,
    now: int,
    recover_expired_reservation: bool = False,
) -> AuthorizationCustody:
    """Consume one detach acknowledgement and move the exclusive attachment."""

    target = Path(target_vault_root)
    current_time = _bounded_time(now)
    external = load_external_custody(target)
    keyring = parse_keyring(external.keyring)
    control = parse_control_record(
        external.control,
        keyring=keyring,
        now=current_time,
    )
    detached = _parse_detach_ack(
        acknowledgement,
        keyring=keyring,
        now=current_time,
        allow_expired=recover_expired_reservation,
    )
    target_attachment = standalone_attachment_id(target)
    if (
        detached.cell_id != keyring.cell_id
        or detached.logical_vault_id != keyring.logical_vault_id
        or detached.keyring_id != keyring.keyring_id
        or detached.target_registry_attachment_id != target_attachment
    ):
        raise AuthorizationCustodyUnavailable
    membership_path, membership_raw, replica_id = _standalone_membership_file(
        target,
        external=external,
    )

    if current_time >= detached.expires_at:
        if not recover_expired_reservation:
            raise AuthorizationCustodyUnavailable
        reservation_started = (
            control.registry_attachment_id
            == detached.target_registry_attachment_id
            and control.attachment_epoch == detached.target_attachment_epoch
        )
        if not reservation_started:
            if (
                control.registry_attachment_id
                != detached.source_registry_attachment_id
                or control.attachment_epoch != detached.source_attachment_epoch
                or control.serving_membership_epoch
                != detached.source_membership_epoch
                or control.serving_membership_digest
                != detached.source_membership_digest
            ):
                raise AuthorizationCustodyUnavailable
            provisional_target = replace(
                control,
                registry_attachment_id=detached.target_registry_attachment_id,
                attachment_epoch=detached.target_attachment_epoch,
                serving_membership_epoch=control.serving_membership_epoch + 1,
                serving_membership_digest="0" * 64,
            )
            try:
                _standalone_membership_successor(
                    membership_raw,
                    keyring=keyring,
                    expected_control=control,
                    target_control=provisional_target,
                    replica_id=replica_id,
                    target_state="DRAINING",
                    target_no_in_flight=True,
                    now=current_time,
                )
            except AuthorizationCustodyUnavailable:
                raise AuthorizationCustodyUnavailable from None
            reservation_started = True
        if not reservation_started:
            raise AuthorizationCustodyUnavailable

    if (
        control.registry_attachment_id == detached.target_registry_attachment_id
        and control.attachment_epoch == detached.target_attachment_epoch
    ):
        if (
            control.serving_membership_epoch
            != detached.source_membership_epoch + 1
            or control.cell_id != detached.cell_id
            or control.logical_vault_id != detached.logical_vault_id
            or control.keyring_id != detached.keyring_id
        ):
            raise AuthorizationCustodyUnavailable
        _require_attachment_target_authority(
            target,
            control=control,
            now=current_time,
            invalidate_sessions=False,
        )
        successor = _parse_migration_membership(
            membership_raw,
            keyring=keyring,
            control=control,
            now=(
                _historical_membership_verification_time(
                    membership_raw,
                    now=current_time,
                )
                if current_time >= detached.expires_at
                else current_time
            ),
            epoch=control.serving_membership_epoch,
            digest=control.serving_membership_digest,
        )
        if successor.previous_epoch_digest != detached.source_membership_digest:
            raise AuthorizationCustodyUnavailable
        _require_standalone_membership(
            successor,
            keyring=keyring,
            control=control,
            replica_id=replica_id,
            state=target_state,
            no_in_flight=target_no_in_flight,
        )
        source_control = replace(
            control,
            registry_attachment_id=detached.source_registry_attachment_id,
            attachment_epoch=detached.source_attachment_epoch,
            serving_membership_epoch=detached.source_membership_epoch,
            serving_membership_digest=detached.source_membership_digest,
        )
        _advance_host_registry(
            source_control=source_control,
            source_state="DRAINING",
            source_no_in_flight=True,
            target_control=control,
            target_state=target_state,
            target_no_in_flight=target_no_in_flight,
            now=current_time,
        )
        if current_time >= detached.expires_at:
            return AuthorizationCustody(
                keyring_path=external.keyring_path,
                control_path=external.control_path,
                keyring=keyring,
                control=control,
                serving_membership=successor,
                local_replica_id=replica_id,
                membership_path=membership_path,
            )
        return load_authorization_custody(target, now=current_time)

    if (
        control.registry_attachment_id
        != detached.source_registry_attachment_id
        or control.attachment_epoch != detached.source_attachment_epoch
        or control.serving_membership_epoch != detached.source_membership_epoch
        or control.serving_membership_digest != detached.source_membership_digest
        or hashlib.sha256(external.control).hexdigest()
        != detached.source_control_digest
        or control.cell_id != detached.cell_id
        or control.logical_vault_id != detached.logical_vault_id
        or control.keyring_id != detached.keyring_id
    ):
        raise AuthorizationCustodyUnavailable
    _require_attachment_target_authority(
        target,
        control=control,
        now=current_time,
        invalidate_sessions=True,
    )

    provisional_target = replace(
        control,
        registry_attachment_id=detached.target_registry_attachment_id,
        attachment_epoch=detached.target_attachment_epoch,
        serving_membership_epoch=control.serving_membership_epoch + 1,
        serving_membership_digest="0" * 64,
    )
    source_record: ServingMembershipEpoch | None
    try:
        source_record = _parse_migration_membership(
            membership_raw,
            keyring=keyring,
            control=control,
            now=current_time,
            epoch=control.serving_membership_epoch,
            digest=control.serving_membership_digest,
        )
    except AuthorizationCustodyUnavailable:
        source_record = None
    if source_record is None:
        successor = _standalone_membership_successor(
            membership_raw,
            keyring=keyring,
            expected_control=control,
            target_control=provisional_target,
            replica_id=replica_id,
            target_state=target_state,
            target_no_in_flight=target_no_in_flight,
            now=current_time,
        )
    else:
        if source_record.record_digest != detached.source_membership_digest:
            raise AuthorizationCustodyUnavailable
        _require_standalone_membership(
            source_record,
            keyring=keyring,
            control=control,
            replica_id=replica_id,
            state="DRAINING",
            no_in_flight=True,
        )
        target_membership = _standalone_membership_bytes(
            keyring=keyring,
            control=provisional_target,
            replica_id=replica_id,
            state=target_state,
            issuance_stopped=target_state == "DRAINING",
            no_in_flight=target_no_in_flight,
            previous_epoch_digest=source_record.record_digest,
            attested_at=current_time,
        )
        successor = _standalone_membership_successor(
            target_membership,
            keyring=keyring,
            expected_control=control,
            target_control=provisional_target,
            replica_id=replica_id,
            target_state=target_state,
            target_no_in_flight=target_no_in_flight,
            now=current_time,
        )
        try:
            authorization_serving_membership.validate_membership_successor(
                source_record,
                successor,
                now=current_time,
            )
        except authorization_serving_membership.ServingMembershipUnavailable:
            raise AuthorizationCustodyUnavailable from None
        _replace_control_bytes(
            membership_path,
            expected=membership_raw,
            target=target_membership,
        )

    target_control = replace(
        provisional_target,
        serving_membership_digest=successor.record_digest,
    )
    signing_key = next(
        (
            item.key
            for item in keyring.accepted_keys
            if item.key_id == target_control.signing_key_id
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
    _advance_host_registry(
        source_control=control,
        source_state="DRAINING",
        source_no_in_flight=True,
        target_control=target_control,
        target_state=target_state,
        target_no_in_flight=target_no_in_flight,
        now=current_time,
    )
    if current_time >= detached.expires_at:
        return AuthorizationCustody(
            keyring_path=external.keyring_path,
            control_path=external.control_path,
            keyring=keyring,
            control=target_control,
            serving_membership=successor,
            local_replica_id=replica_id,
            membership_path=membership_path,
        )
    verified = load_authorization_custody(target, now=current_time)
    if verified.control != target_control or verified.serving_membership is None:
        raise AuthorizationCustodyUnavailable
    return verified


def complete_standalone_attachment_transfer(
    target_vault_root: Path,
    *,
    acknowledgement: bytes,
    now: int,
) -> AuthorizationCustody:
    """Consume one detach acknowledgement under the target identity fence."""

    target = Path(target_vault_root)
    from .. import reserved_paths, writer_lease

    with writer_lease.get_manager().mutation_guard(
        target,
        operation="authorization-attachment-transfer",
        holder_kind="authorization-attachment-control",
        attachment_control=True,
        attachment_now=now,
    ):
        with reserved_paths._identity_coordination_scope(target):
            return _complete_standalone_attachment_transfer(
                target,
                acknowledgement=acknowledgement,
                target_state="SERVING",
                target_no_in_flight=False,
                now=now,
            )


def reserve_standalone_attachment_transfer(
    target_vault_root: Path,
    *,
    acknowledgement: bytes,
    now: int,
    recover_expired_reservation: bool = False,
) -> AuthorizationCustody:
    """Move attachment authority to an exact target without serving it."""

    target = Path(target_vault_root)
    from .. import reserved_paths, writer_lease

    with writer_lease.get_manager().mutation_guard(
        target,
        operation="authorization-attachment-transfer-reserve",
        holder_kind="authorization-attachment-control",
        attachment_control=True,
        attachment_now=now,
    ):
        with reserved_paths._identity_coordination_scope(target):
            return _complete_standalone_attachment_transfer(
                target,
                acknowledgement=acknowledgement,
                target_state="DRAINING",
                target_no_in_flight=True,
                now=now,
                recover_expired_reservation=recover_expired_reservation,
            )


def activate_reserved_standalone_attachment(
    target_vault_root: Path,
    *,
    expected_control: AuthorizationControlRecord,
    now: int,
    recover_expired_reservation: bool = False,
) -> AuthorizationCustody:
    """Activate one exact already-reserved target attachment."""

    target = Path(target_vault_root)
    from .. import writer_lease

    with writer_lease.get_manager().mutation_guard(
        target,
        operation="authorization-attachment-transfer-activate",
        holder_kind="authorization-attachment-control",
        attachment_control=True,
        attachment_now=now,
    ):
        return _transition_standalone_attachment_membership(
            target,
            expected_control=expected_control,
            source_state="DRAINING",
            source_no_in_flight=True,
            target_state="SERVING",
            target_no_in_flight=False,
            now=now,
            recover_expired_source=recover_expired_reservation,
        )


def _clone_publication_barrier(point: str) -> None:
    """Crash-injection seam between durable exact-v4 clone effects."""

    del point


def _optional_custody_file(path: Path) -> _LoadedCustodyFile | None:
    try:
        return _load_file(path)
    except AuthorizationCustodyUnavailable:
        if os.path.lexists(path):
            raise
        return None


def _clone_publication_event_id(
    keyring: AuthorizationKeyring,
    *,
    attachment_id: str,
) -> str:
    return _standalone_staging_identifier(
        "attachment-clone",
        attachment_id=attachment_id,
        key=keyring.active_key.key,
    )


def _clone_receipt_identity(
    keyring: AuthorizationKeyring,
    *,
    attachment_id: str,
) -> tuple[str, bytes]:
    material = _framed(
        _STANDALONE_CLONE_RECEIPT_DOMAIN,
        (attachment_id.encode("ascii"),),
    )
    instance_id = hmac.new(
        keyring.active_key.key,
        material + b"\0instance",
        hashlib.sha256,
    ).hexdigest()[:32]
    label_secret = hmac.new(
        keyring.active_key.key,
        material + b"\0label-hmac",
        hashlib.sha256,
    ).digest()
    return instance_id, label_secret


def _clone_standalone_exact_v4_custody(
    vault_root: Path,
    *,
    now: int,
) -> AuthorizationCustody:
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

    keyring_loaded = _optional_custody_file(keyring_path)
    control_loaded = _optional_custody_file(control_path)
    membership_loaded = _optional_custody_file(membership_path)
    if (
        (control_loaded is not None and keyring_loaded is None)
        or (membership_loaded is not None and control_loaded is None)
    ):
        raise AuthorizationCustodyUnavailable

    from . import schema_v4, store

    if keyring_loaded is None:
        try:
            preflight = store.open_authorization_session_connection(root)
            try:
                schema_v4.require_exact_v4_connection(preflight)
            finally:
                preflight.close()
        except (
            OSError,
            RuntimeError,
            schema_v4.SchemaV4Error,
            store.UnsupportedGovernanceSchema,
        ):
            raise AuthorizationCustodyUnavailable from None
        keyring = _new_standalone_staging_keyring(
            attachment_id=attachment_id,
            current_time=current_time,
        )
        _publish_private_file(keyring_path, _keyring_bytes(keyring))
    else:
        keyring = parse_keyring(keyring_loaded.data)
    _verify_standalone_staging_keyring(keyring, attachment_id=attachment_id)
    if not keyring.active_key.not_before <= current_time < keyring.active_key.not_after:
        raise AuthorizationCustodyUnavailable
    _clone_publication_barrier("after-staged-keyring")

    activation_store_id = _standalone_staging_identifier(
        "activation-store",
        attachment_id=attachment_id,
        key=keyring.active_key.key,
    )
    receipt_instance_id, receipt_label_secret = _clone_receipt_identity(
        keyring,
        attachment_id=attachment_id,
    )
    try:
        connection = store.open_authorization_session_connection(root)
        try:
            active = schema_v4.clone_attachment_identity(
                connection,
                logical_vault_id=keyring.logical_vault_id,
                activation_store_id=activation_store_id,
                publication_event_id=_clone_publication_event_id(
                    keyring,
                    attachment_id=attachment_id,
                ),
                receipt_instance_id=receipt_instance_id,
                receipt_label_secret=receipt_label_secret,
                activated_at=keyring.active_key.not_before,
            )
        finally:
            connection.close()
    except (
        OSError,
        RuntimeError,
        schema_v4.SchemaV4Error,
        store.UnsupportedGovernanceSchema,
    ):
        raise AuthorizationCustodyUnavailable from None
    _clone_publication_barrier("after-clone-transaction")

    provisional_control = AuthorizationControlRecord(
        version=1,
        keyring_id=keyring.keyring_id,
        cell_id=keyring.cell_id,
        logical_vault_id=keyring.logical_vault_id,
        registry_attachment_id=attachment_id,
        attachment_epoch=1,
        governance_enrolled=True,
        activation_store_id=active.activation_store_id,
        activation_epoch=active.activation_epoch,
        activation_state_digest=active.activation_state_digest,
        serving_membership_epoch=1,
        serving_membership_digest="0" * 64,
        issued_at=keyring.active_key.not_before,
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
    if control_loaded is None:
        _publish_private_file(control_path, encoded_control)
    elif not hmac.compare_digest(control_loaded.data, encoded_control):
        raise AuthorizationCustodyUnavailable
    _clone_publication_barrier("after-control")
    if membership_loaded is None:
        _publish_private_file(membership_path, encoded_membership)
    elif not hmac.compare_digest(membership_loaded.data, encoded_membership):
        raise AuthorizationCustodyUnavailable
    _clone_publication_barrier("after-membership")
    _publish_initial_host_registry(control, now=current_time)
    _clone_publication_barrier("after-registry")

    verified = load_authorization_custody(root, now=current_time)
    if verified.control != control or verified.serving_membership is None:
        raise AuthorizationCustodyUnavailable
    return verified


def clone_standalone_exact_v4_custody(
    vault_root: Path,
    *,
    now: int,
) -> AuthorizationCustody:
    """Give an unattached exact-v4 copy one fresh standalone identity."""

    root = Path(vault_root)
    from .. import reserved_paths, writer_lease
    from . import receipts

    with writer_lease.get_manager().mutation_guard(
        root,
        operation="authorization-attachment-clone",
        holder_kind="authorization-attachment-control",
        attachment_control=True,
        attachment_now=now,
    ):
        with receipts._receipt_lock(root):  # noqa: SLF001
            try:
                receipt_report = receipts.verify_chain(root)
            except (OSError, RuntimeError, TypeError, ValueError):
                raise AuthorizationCustodyUnavailable from None
            if receipt_report.get("valid") is not True:
                raise AuthorizationCustodyUnavailable
            with reserved_paths._identity_coordination_scope(root):
                return _clone_standalone_exact_v4_custody(root, now=now)


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
        _publish_initial_host_registry(control, now=current_time)

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
