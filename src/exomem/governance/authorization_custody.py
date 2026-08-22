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
_SHA256_HEX = re.compile(r"[0-9a-f]{64}\Z")


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


def _prepare_private_control_stage(control_path: Path, staged: held_fs.HeldFile) -> None:
    descriptor = getattr(staged, "descriptor", None)
    if not isinstance(descriptor, int):
        raise AuthorizationCustodyUnavailable
    if os.name == "nt":
        name = getattr(staged, "name", None)
        if not isinstance(name, str) or not name:
            raise AuthorizationCustodyUnavailable
        mutation_lock._windows_apply_private_dacl(
            control_path.parent / name,
            mutation_lock._windows_current_user_sid(),
        )
    info = os.fstat(descriptor)
    if not _file_is_owner_protected(descriptor, info):
        raise AuthorizationCustodyUnavailable


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
                    prepare=lambda staged: _prepare_private_control_stage(path, staged),
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

    external = load_external_custody(Path(vault_root))
    keyring = parse_keyring(external.keyring)
    current = parse_control_record(external.control, keyring=keyring, now=now)
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
    verified = load_authorization_custody(Path(vault_root), now=now)
    if verified.control != target_control:
        raise AuthorizationCustodyUnavailable
    return acknowledgement
