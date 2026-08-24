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
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from exomem import held_fs, mutation_lock

if TYPE_CHECKING:
    from .schema_v4 import (
        ActivationRegistryAcknowledgement,
        VerifiedActiveGovernanceState,
    )

KEYRING_FILE_ENV = "EXOMEM_AUTH_SESSION_KEYRING_FILE"
CONTROL_FILE_ENV = "EXOMEM_AUTH_SESSION_CONTROL_FILE"
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
_BOOTSTRAP_MEMBERSHIP_DOMAIN = b"exomem.authorization-session.membership-bootstrap/v1"
_SHA256_HEX = re.compile(r"[0-9a-f]{64}\Z")
_STANDALONE_ATTACHMENT = re.compile(r"attachment-v1-[0-9a-f]{64}\Z")
_DEFAULT_CONTROL_TTL_SECONDS = 3_600
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


@dataclass(frozen=True, slots=True)
class StandaloneProvisioningResult:
    """Non-secret identity returned by explicit standalone provisioning."""

    keyring_path: Path
    control_path: Path
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
    return AuthorizationCustody(
        keyring_path=external.keyring_path,
        control_path=external.control_path,
        keyring=keyring,
        control=control,
    )


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


def _opaque_identifier(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(16)}"


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


def _bootstrap_membership_digest(
    *,
    cell_id: str,
    logical_vault_id: str,
    attachment_id: str,
    keyring_id: str,
    active_key_id: str,
) -> str:
    return hashlib.sha256(
        _framed(
            _BOOTSTRAP_MEMBERSHIP_DOMAIN,
            tuple(
                value.encode("utf-8")
                for value in (
                    cell_id,
                    logical_vault_id,
                    attachment_id,
                    keyring_id,
                    active_key_id,
                )
            ),
        )
    ).hexdigest()


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


def _provisioning_result(
    custody: AuthorizationCustody,
) -> StandaloneProvisioningResult:
    return StandaloneProvisioningResult(
        keyring_path=custody.keyring_path,
        control_path=custody.control_path,
        keyring_id=custody.keyring.keyring_id,
        cell_id=custody.keyring.cell_id,
        logical_vault_id=custody.keyring.logical_vault_id,
        registry_attachment_id=custody.control.registry_attachment_id,
        attachment_epoch=custody.control.attachment_epoch,
    )


def provision_standalone_custody(
    vault_root: Path,
    *,
    now: int,
) -> StandaloneProvisioningResult:
    """Explicitly provision one never-enrolled standalone attachment.

    This is never called from an ordinary loader.  It creates no vault state;
    the externally authenticated control record is the final registration
    point, and a keyring-only interruption can be retried without rotating the
    generated identity.
    """

    root = Path(vault_root)
    current_time = _bounded_time(now)
    if current_time > _MAX_SIGNED_SQLITE_INTEGER - _DEFAULT_KEY_TTL_SECONDS:
        raise AuthorizationCustodyUnavailable
    attachment_id = standalone_attachment_id(root)
    keyring_path = _configured_external_path(KEYRING_FILE_ENV, root)
    control_path = _configured_external_path(CONTROL_FILE_ENV, root)
    if keyring_path == control_path:
        raise AuthorizationCustodyUnavailable

    from .. import reserved_paths

    # First attachment is the one operation that must prove the vault has no
    # private owner state at all.  Take the registry-wide identity guard rather
    # than pretending to hold dispatcher authority for two different owners.
    with reserved_paths._identity_coordination_scope(root):
        _governance_negative_scan(root)
        keyring_loaded: _LoadedCustodyFile | None = None
        control_loaded: _LoadedCustodyFile | None = None
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

        if control_loaded is not None and keyring_loaded is None:
            raise AuthorizationCustodyUnavailable
        if keyring_loaded is not None:
            keyring = parse_keyring(keyring_loaded.data)
        else:
            key = AuthorizationVerifierKey(
                key_id=_opaque_identifier("auth-key"),
                key=secrets.token_bytes(32),
                not_before=current_time,
                not_after=current_time + _DEFAULT_KEY_TTL_SECONDS,
            )
            keyring = AuthorizationKeyring(
                version=1,
                keyring_id=_opaque_identifier("keyring"),
                cell_id=_opaque_identifier("cell"),
                logical_vault_id=_opaque_identifier("vault"),
                active_key_id=key.key_id,
                accepted_keys=(key,),
            )
            encoded_keyring = _keyring_bytes(keyring)
            _publish_private_file(keyring_path, encoded_keyring)

        if not keyring.active_key.not_before <= current_time < keyring.active_key.not_after:
            raise AuthorizationCustodyUnavailable
        if control_loaded is None:
            membership_digest = _bootstrap_membership_digest(
                cell_id=keyring.cell_id,
                logical_vault_id=keyring.logical_vault_id,
                attachment_id=attachment_id,
                keyring_id=keyring.keyring_id,
                active_key_id=keyring.active_key_id,
            )
            control = AuthorizationControlRecord(
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
                serving_membership_digest=membership_digest,
                issued_at=current_time,
                expires_at=min(
                    current_time + _DEFAULT_CONTROL_TTL_SECONDS,
                    keyring.active_key.not_after,
                ),
                signing_key_id=keyring.active_key_id,
            )
            encoded_control = _signed_control_bytes(
                control,
                signing_key=keyring.active_key.key,
            )
            _governance_negative_scan(root)
            _publish_private_file(control_path, encoded_control)

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
        _governance_negative_scan(root)
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
