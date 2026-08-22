from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import stat
from pathlib import Path

import pytest

from exomem.governance import authorization_custody, schema_v4


@pytest.fixture(autouse=True)
def _clear_custody_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(authorization_custody.KEYRING_FILE_ENV, raising=False)
    monkeypatch.delenv(authorization_custody.CONTROL_FILE_ENV, raising=False)


def _protected_file(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    if os.name == "nt":
        from exomem import mutation_lock

        mutation_lock._windows_apply_private_dacl(
            path, mutation_lock._windows_current_user_sid()
        )
    else:
        path.chmod(0o600)
    return path


def _configure(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    *,
    keyring: bytes = b'{"kind":"keyring-sentinel"}',
    control: bytes = b'{"kind":"control-sentinel"}',
) -> tuple[Path, Path]:
    keyring_path = _protected_file(root / "authorization-keyring.json", keyring)
    control_path = _protected_file(root / "authorization-control.json", control)
    monkeypatch.setenv(authorization_custody.KEYRING_FILE_ENV, str(keyring_path))
    monkeypatch.setenv(authorization_custody.CONTROL_FILE_ENV, str(control_path))
    return keyring_path, control_path


def _keyring_document(**changes: object) -> bytes:
    document: dict[str, object] = {
        "version": 1,
        "keyring_id": "keyring-e7901e43",
        "cell_id": "cell-7bd27031",
        "logical_vault_id": "vault-a2699d30",
        "active_key_id": "auth-key-2026-08",
        "accepted_keys": [
            {
                "key_id": "auth-key-2026-08",
                "key": base64.urlsafe_b64encode(b"k" * 32)
                .rstrip(b"=")
                .decode("ascii"),
                "not_before": 1_800_000_000,
                "not_after": 1_800_086_400,
            }
        ],
    }
    document.update(changes)
    return json.dumps(document, separators=(",", ":")).encode()


def _framed(domain: bytes, fields: list[bytes]) -> bytes:
    output = bytearray(domain)
    output.append(0)
    for field in fields:
        output.extend(len(field).to_bytes(4, "big"))
        output.extend(field)
    return bytes(output)


def _control_document(*, signing_key: bytes = b"k" * 32, **changes: object) -> bytes:
    document: dict[str, object] = {
        "version": 1,
        "keyring_id": "keyring-e7901e43",
        "cell_id": "cell-7bd27031",
        "logical_vault_id": "vault-a2699d30",
        "registry_attachment_id": "attachment-5d998951",
        "attachment_epoch": 7,
        "governance_enrolled": False,
        "activation_store_id": None,
        "activation_epoch": None,
        "activation_state_digest": None,
        "serving_membership_epoch": 11,
        "serving_membership_digest": "b" * 64,
        "issued_at": 1_800_000_000,
        "expires_at": 1_800_003_600,
        "signing_key_id": "auth-key-2026-08",
    }
    document.update(changes)
    fields = [
        str(document["version"]).encode(),
        str(document["keyring_id"]).encode(),
        str(document["cell_id"]).encode(),
        str(document["logical_vault_id"]).encode(),
        str(document["registry_attachment_id"]).encode(),
        str(document["attachment_epoch"]).encode(),
        b"true" if document["governance_enrolled"] is True else b"false",
        b"" if document["activation_store_id"] is None else str(document["activation_store_id"]).encode(),
        b"" if document["activation_epoch"] is None else str(document["activation_epoch"]).encode(),
        b"" if document["activation_state_digest"] is None else str(document["activation_state_digest"]).encode(),
        str(document["serving_membership_epoch"]).encode(),
        str(document["serving_membership_digest"]).encode(),
        str(document["issued_at"]).encode(),
        str(document["expires_at"]).encode(),
        str(document["signing_key_id"]).encode(),
    ]
    mac = hmac.new(
        signing_key,
        _framed(b"exomem.authorization-session.control/v1", fields),
        hashlib.sha256,
    ).digest()
    document["mac"] = base64.urlsafe_b64encode(mac).rstrip(b"=").decode()
    return json.dumps(document, separators=(",", ":")).encode()


def test_activation_registry_acknowledgement_is_exact_atomic_and_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 1_800_000_100
    prior_digest = "1" * 64
    target_digest = "2" * 64
    keyring_path, control_path = _configure(
        monkeypatch,
        tmp_path / "external" / "custody",
        keyring=_keyring_document(),
        control=_control_document(
            governance_enrolled=True,
            activation_store_id="activation-store-7",
            activation_epoch=7,
            activation_state_digest=prior_digest,
        ),
    )
    vault = tmp_path / "vault"
    expected = authorization_custody.load_authorization_custody(vault, now=now)
    target = schema_v4.VerifiedActiveGovernanceState(
        logical_vault_id=expected.control.logical_vault_id,
        activation_store_id="activation-store-7",
        activation_epoch=8,
        activation_state_digest=target_digest,
        policy_generation_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
        policy_fingerprint="3" * 64,
        projector_schema_version=1,
        catalog_generation=2,
        projection_namespace_id="namespace-2",
    )
    before_keyring = keyring_path.read_bytes()

    first = authorization_custody.acknowledge_activation_tuple(
        vault,
        expected_control=expected.control,
        target=target,
        now=now,
    )
    replay = authorization_custody.acknowledge_activation_tuple(
        vault,
        expected_control=expected.control,
        target=target,
        now=now,
    )
    loaded = authorization_custody.load_authorization_custody(vault, now=now)

    assert first == replay == schema_v4.ActivationRegistryAcknowledgement(
        activation_store_id="activation-store-7",
        activation_epoch=8,
        activation_state_digest=target_digest,
    )
    assert loaded.control.activation_epoch == 8
    assert loaded.control.activation_state_digest == target_digest
    assert keyring_path.read_bytes() == before_keyring
    assert control_path.stat().st_nlink == 1
    if os.name != "nt":
        assert stat.S_IMODE(control_path.stat().st_mode) == 0o600

    stale = schema_v4.VerifiedActiveGovernanceState(
        logical_vault_id=target.logical_vault_id,
        activation_store_id=target.activation_store_id,
        activation_epoch=8,
        activation_state_digest="4" * 64,
        policy_generation_id=target.policy_generation_id,
        policy_fingerprint=target.policy_fingerprint,
        projector_schema_version=target.projector_schema_version,
        catalog_generation=target.catalog_generation,
        projection_namespace_id=target.projection_namespace_id,
    )
    committed = control_path.read_bytes()
    with pytest.raises(authorization_custody.AuthorizationCustodyUnavailable):
        authorization_custody.acknowledge_activation_tuple(
            vault,
            expected_control=expected.control,
            target=stale,
            now=now,
        )
    assert control_path.read_bytes() == committed


@pytest.mark.parametrize(
    "missing",
    (
        authorization_custody.KEYRING_FILE_ENV,
        authorization_custody.CONTROL_FILE_ENV,
    ),
)
def test_both_external_custody_paths_are_mandatory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    _configure(monkeypatch, tmp_path / "external")
    monkeypatch.delenv(missing)

    with pytest.raises(authorization_custody.AuthorizationCustodyUnavailable) as raised:
        authorization_custody.load_external_custody(tmp_path / "vault")

    assert raised.value.code == "AUTHORIZATION_SESSION_UNAVAILABLE"
    assert missing not in str(raised.value)


def test_custody_loader_never_generates_missing_first_use_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    external = tmp_path / "external"
    keyring_path = external / "authorization-keyring.json"
    control_path = external / "authorization-control.json"
    monkeypatch.setenv(authorization_custody.KEYRING_FILE_ENV, str(keyring_path))
    monkeypatch.setenv(authorization_custody.CONTROL_FILE_ENV, str(control_path))

    with pytest.raises(authorization_custody.AuthorizationCustodyUnavailable):
        authorization_custody.load_external_custody(tmp_path / "vault")

    assert not external.exists()
    assert not keyring_path.exists()
    assert not control_path.exists()


def test_custody_read_is_bounded_stable_and_bearer_free_in_repr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keyring = b'{"secret":"keyring-secret-sentinel"}'
    control = b'{"signature":"control-signature-sentinel"}'
    keyring_path, control_path = _configure(
        monkeypatch, tmp_path / "external", keyring=keyring, control=control
    )

    loaded = authorization_custody.load_external_custody(tmp_path / "vault")

    assert loaded.keyring == keyring
    assert loaded.control == control
    assert loaded.keyring_path == keyring_path
    assert loaded.control_path == control_path
    assert "keyring-secret-sentinel" not in repr(loaded)
    assert "control-signature-sentinel" not in repr(loaded)


def test_keyring_and_control_must_be_distinct_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keyring_path, _control_path = _configure(monkeypatch, tmp_path / "external")
    monkeypatch.setenv(authorization_custody.CONTROL_FILE_ENV, str(keyring_path))

    with pytest.raises(authorization_custody.AuthorizationCustodyUnavailable):
        authorization_custody.load_external_custody(tmp_path / "vault")


def test_custody_change_during_read_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keyring_path, _control_path = _configure(monkeypatch, tmp_path / "external")
    original = authorization_custody._read_retained

    def change_after_read(descriptor: int, expected_size: int) -> bytes:
        data = original(descriptor, expected_size)
        keyring_path.write_bytes(b"x" * expected_size)
        return data

    monkeypatch.setattr(authorization_custody, "_read_retained", change_after_read)

    with pytest.raises(authorization_custody.AuthorizationCustodyUnavailable):
        authorization_custody.load_external_custody(tmp_path / "vault")


def test_timestamp_preserving_custody_change_during_read_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keyring_path, _control_path = _configure(monkeypatch, tmp_path / "external")
    original = authorization_custody._read_retained

    def change_and_restore_mtime(descriptor: int, expected_size: int) -> bytes:
        data = original(descriptor, expected_size)
        before = keyring_path.stat()
        keyring_path.write_bytes(b"x" * expected_size)
        os.utime(
            keyring_path,
            ns=(before.st_atime_ns, before.st_mtime_ns),
        )
        return data

    monkeypatch.setattr(
        authorization_custody, "_read_retained", change_and_restore_mtime
    )

    with pytest.raises(authorization_custody.AuthorizationCustodyUnavailable):
        authorization_custody.load_external_custody(tmp_path / "vault")


def test_keyring_parser_returns_exact_bearer_free_keys() -> None:
    parsed = authorization_custody.parse_keyring(_keyring_document())

    assert parsed.version == 1
    assert parsed.keyring_id == "keyring-e7901e43"
    assert parsed.cell_id == "cell-7bd27031"
    assert parsed.logical_vault_id == "vault-a2699d30"
    assert parsed.active_key.key_id == "auth-key-2026-08"
    assert parsed.active_key.key == b"k" * 32
    assert (b"k" * 32).hex() not in repr(parsed)


@pytest.mark.parametrize(
    "raw",
    (
        b"",
        b"[]",
        b'{"version":1,"version":1}',
        _keyring_document(version=2),
        _keyring_document(extra="not-allowed"),
        _keyring_document(keyring_id=""),
        _keyring_document(cell_id="x" * 513),
        _keyring_document(active_key_id="missing-key"),
        _keyring_document(accepted_keys=[]),
    ),
)
def test_keyring_parser_rejects_bad_shape_version_or_identity(raw: bytes) -> None:
    with pytest.raises(authorization_custody.AuthorizationCustodyUnavailable):
        authorization_custody.parse_keyring(raw)


def test_keyring_parser_maps_unencodable_identity_to_content_free_refusal() -> None:
    raw = _keyring_document(keyring_id="\ud800")

    with pytest.raises(authorization_custody.AuthorizationCustodyUnavailable):
        authorization_custody.parse_keyring(raw)


@pytest.mark.parametrize(
    "key_value",
    (
        "A" * 42,
        "A" * 44,
        "A" * 42 + "=",
        "A" * 42 + "+",
        "A" * 42 + "/",
        "A" * 42 + "B",
    ),
)
def test_keyring_parser_rejects_noncanonical_or_wrong_length_keys(
    key_value: str,
) -> None:
    entry = json.loads(_keyring_document())["accepted_keys"][0]
    entry["key"] = key_value

    with pytest.raises(authorization_custody.AuthorizationCustodyUnavailable):
        authorization_custody.parse_keyring(
            _keyring_document(accepted_keys=[entry])
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("not_before", True),
        ("not_before", 0),
        ("not_before", 2**63),
        ("not_after", False),
        ("not_after", 0),
        ("not_after", 2**63),
    ),
)
def test_keyring_parser_rejects_unbounded_key_times(field: str, value: object) -> None:
    entry = json.loads(_keyring_document())["accepted_keys"][0]
    entry[field] = value

    with pytest.raises(authorization_custody.AuthorizationCustodyUnavailable):
        authorization_custody.parse_keyring(
            _keyring_document(accepted_keys=[entry])
        )


def test_keyring_parser_rejects_reversed_time_or_duplicate_key_id() -> None:
    entry = json.loads(_keyring_document())["accepted_keys"][0]
    reversed_entry = {**entry, "not_before": 1_800_086_400}
    with pytest.raises(authorization_custody.AuthorizationCustodyUnavailable):
        authorization_custody.parse_keyring(
            _keyring_document(accepted_keys=[reversed_entry])
        )

    with pytest.raises(authorization_custody.AuthorizationCustodyUnavailable):
        authorization_custody.parse_keyring(
            _keyring_document(accepted_keys=[entry, entry])
        )


def test_control_record_authenticates_external_identity_and_enrollment() -> None:
    keyring = authorization_custody.parse_keyring(_keyring_document())

    control = authorization_custody.parse_control_record(
        _control_document(), keyring=keyring, now=1_800_000_100
    )

    assert control.cell_id == keyring.cell_id
    assert control.logical_vault_id == keyring.logical_vault_id
    assert control.keyring_id == keyring.keyring_id
    assert control.registry_attachment_id == "attachment-5d998951"
    assert control.attachment_epoch == 7
    assert control.governance_enrolled is False
    assert control.activation_store_id is None
    assert control.serving_membership_epoch == 11
    assert control.signing_key_id == "auth-key-2026-08"
    assert "a2tra2" not in repr(control)


def test_enrolled_control_requires_the_complete_activation_tuple() -> None:
    keyring = authorization_custody.parse_keyring(_keyring_document())
    raw = _control_document(
        governance_enrolled=True,
        activation_store_id="activation-3eb50845",
        activation_epoch=4,
        activation_state_digest="a" * 64,
    )

    control = authorization_custody.parse_control_record(
        raw, keyring=keyring, now=1_800_000_100
    )

    assert control.governance_enrolled is True
    assert control.activation_store_id == "activation-3eb50845"
    assert control.activation_epoch == 4
    assert control.activation_state_digest == "a" * 64


@pytest.mark.parametrize(
    "changes",
    (
        {"cell_id": "cell-other"},
        {"logical_vault_id": "vault-other"},
        {"keyring_id": "keyring-other"},
        {"signing_key_id": "unknown-key"},
        {"attachment_epoch": 0},
        {"serving_membership_epoch": 0},
        {"serving_membership_digest": "z" * 64},
        {"governance_enrolled": True},
        {"activation_store_id": "activation-unexpected"},
        {"activation_epoch": 1},
        {"activation_state_digest": "a" * 64},
    ),
)
def test_control_record_rejects_identity_or_enrollment_mismatch(
    changes: dict[str, object],
) -> None:
    keyring = authorization_custody.parse_keyring(_keyring_document())

    with pytest.raises(authorization_custody.AuthorizationCustodyUnavailable):
        authorization_custody.parse_control_record(
            _control_document(**changes), keyring=keyring, now=1_800_000_100
        )


@pytest.mark.parametrize("now", (1_799_999_999, 1_800_003_600, True))
def test_control_record_rejects_outside_its_and_the_signing_keys_window(
    now: object,
) -> None:
    keyring = authorization_custody.parse_keyring(_keyring_document())

    with pytest.raises(authorization_custody.AuthorizationCustodyUnavailable):
        authorization_custody.parse_control_record(
            _control_document(), keyring=keyring, now=now  # type: ignore[arg-type]
        )


def test_control_record_rejects_bad_mac_duplicate_or_extra_field() -> None:
    keyring = authorization_custody.parse_keyring(_keyring_document())
    document = json.loads(_control_document())
    document["mac"] = "A" * 43
    bad_mac = json.dumps(document, separators=(",", ":")).encode()
    with pytest.raises(authorization_custody.AuthorizationCustodyUnavailable):
        authorization_custody.parse_control_record(
            bad_mac, keyring=keyring, now=1_800_000_100
        )

    with pytest.raises(authorization_custody.AuthorizationCustodyUnavailable):
        authorization_custody.parse_control_record(
            b'{"version":1,"version":1}',
            keyring=keyring,
            now=1_800_000_100,
        )

    document = json.loads(_control_document())
    document["extra"] = "not-allowed"
    with pytest.raises(authorization_custody.AuthorizationCustodyUnavailable):
        authorization_custody.parse_control_record(
            json.dumps(document).encode(),
            keyring=keyring,
            now=1_800_000_100,
        )


@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        ("version", 2),
        ("keyring_id", "keyring-other"),
        ("cell_id", "cell-other"),
        ("logical_vault_id", "vault-other"),
        ("registry_attachment_id", "attachment-other"),
        ("attachment_epoch", 8),
        ("governance_enrolled", False),
        ("activation_store_id", "activation-other"),
        ("activation_epoch", 5),
        ("activation_state_digest", "b" * 64),
        ("serving_membership_epoch", 12),
        ("serving_membership_digest", "c" * 64),
        ("issued_at", 1_800_000_001),
        ("expires_at", 1_800_003_599),
        ("signing_key_id", "unknown-key"),
    ),
)
def test_control_mac_or_validation_binds_every_non_mac_field(
    field: str,
    replacement: object,
) -> None:
    keyring = authorization_custody.parse_keyring(_keyring_document())
    document = json.loads(
        _control_document(
            governance_enrolled=True,
            activation_store_id="activation-3eb50845",
            activation_epoch=4,
            activation_state_digest="a" * 64,
        )
    )
    document[field] = replacement

    with pytest.raises(authorization_custody.AuthorizationCustodyUnavailable):
        authorization_custody.parse_control_record(
            json.dumps(document, separators=(",", ":")).encode(),
            keyring=keyring,
            now=1_800_000_100,
        )


def test_control_record_requires_the_active_issuance_key_to_be_current() -> None:
    expired_active = json.loads(_keyring_document())["accepted_keys"][0]
    expired_active["not_after"] = 1_800_000_050
    control_signing = {
        "key_id": "control-key-2026-08",
        "key": base64.urlsafe_b64encode(b"s" * 32)
        .rstrip(b"=")
        .decode("ascii"),
        "not_before": 1_800_000_000,
        "not_after": 1_800_086_400,
    }
    keyring = authorization_custody.parse_keyring(
        _keyring_document(accepted_keys=[expired_active, control_signing])
    )

    with pytest.raises(authorization_custody.AuthorizationCustodyUnavailable):
        authorization_custody.parse_control_record(
            _control_document(
                signing_key=b"s" * 32,
                signing_key_id="control-key-2026-08",
            ),
            keyring=keyring,
            now=1_800_000_100,
        )


def test_windows_custody_requires_a_protected_private_file_dacl() -> None:
    from exomem import mutation_lock

    sid = "S-1-5-21-9-9-9-1001"
    protected = mutation_lock._windows_private_dacl_sddl(sid)
    inherited = protected.replace("D:P", "D:", 1)

    assert authorization_custody._windows_file_dacl_is_private(protected, sid)
    assert not authorization_custody._windows_file_dacl_is_private(inherited, sid)


def test_authenticated_custody_loader_returns_only_verified_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keyring_path, control_path = _configure(
        monkeypatch,
        tmp_path / "external",
        keyring=_keyring_document(),
        control=_control_document(),
    )

    loaded = authorization_custody.load_authorization_custody(
        tmp_path / "vault", now=1_800_000_100
    )

    assert loaded.keyring_path == keyring_path
    assert loaded.control_path == control_path
    assert loaded.keyring.active_key.key == b"k" * 32
    assert loaded.control.registry_attachment_id == "attachment-5d998951"
    assert (b"k" * 32).hex() not in repr(loaded)
    assert "mac" not in repr(loaded)


def test_authenticated_custody_loader_never_returns_unverified_control(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    control = json.loads(_control_document())
    control["mac"] = "A" * 43
    _configure(
        monkeypatch,
        tmp_path / "external",
        keyring=_keyring_document(),
        control=json.dumps(control, separators=(",", ":")).encode(),
    )

    with pytest.raises(authorization_custody.AuthorizationCustodyUnavailable):
        authorization_custody.load_authorization_custody(
            tmp_path / "vault", now=1_800_000_100
        )


@pytest.mark.parametrize("configured_name", ("relative.json", ""))
def test_relative_or_empty_custody_path_refuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    configured_name: str,
) -> None:
    _configure(monkeypatch, tmp_path / "external")
    monkeypatch.setenv(authorization_custody.KEYRING_FILE_ENV, configured_name)

    with pytest.raises(authorization_custody.AuthorizationCustodyUnavailable):
        authorization_custody.load_external_custody(tmp_path / "vault")


@pytest.mark.parametrize("kind", ("keyring", "control"))
def test_custody_path_inside_the_vault_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    vault = tmp_path / "vault"
    keyring_path, control_path = _configure(monkeypatch, tmp_path / "external")
    selected = _protected_file(vault / f"{kind}.json", b"{}")
    monkeypatch.setenv(
        authorization_custody.KEYRING_FILE_ENV
        if kind == "keyring"
        else authorization_custody.CONTROL_FILE_ENV,
        str(selected),
    )
    assert keyring_path != selected and control_path != selected

    with pytest.raises(authorization_custody.AuthorizationCustodyUnavailable):
        authorization_custody.load_external_custody(vault)


@pytest.mark.parametrize("kind", ("keyring", "control"))
def test_symlinked_custody_file_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    keyring_path, control_path = _configure(monkeypatch, tmp_path / "external")
    target = keyring_path if kind == "keyring" else control_path
    link = target.with_name(f"{target.stem}-link.json")
    try:
        link.symlink_to(target)
    except OSError as error:
        pytest.skip(f"symlinks unavailable: {error}")
    monkeypatch.setenv(
        authorization_custody.KEYRING_FILE_ENV
        if kind == "keyring"
        else authorization_custody.CONTROL_FILE_ENV,
        str(link),
    )

    with pytest.raises(authorization_custody.AuthorizationCustodyUnavailable):
        authorization_custody.load_external_custody(tmp_path / "vault")


@pytest.mark.parametrize("kind", ("keyring", "control"))
def test_directory_or_oversized_custody_file_refuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    keyring_path, control_path = _configure(monkeypatch, tmp_path / "external")
    selected = keyring_path if kind == "keyring" else control_path
    selected.unlink()
    selected.mkdir()
    monkeypatch.setenv(
        authorization_custody.KEYRING_FILE_ENV
        if kind == "keyring"
        else authorization_custody.CONTROL_FILE_ENV,
        str(selected),
    )
    with pytest.raises(authorization_custody.AuthorizationCustodyUnavailable):
        authorization_custody.load_external_custody(tmp_path / "vault")

    selected.rmdir()
    _protected_file(
        selected,
        b"x" * (authorization_custody.MAX_CUSTODY_FILE_BYTES + 1),
    )
    with pytest.raises(authorization_custody.AuthorizationCustodyUnavailable):
        authorization_custody.load_external_custody(tmp_path / "vault")


@pytest.mark.skipif(os.name == "nt", reason="POSIX owner/mode contract")
@pytest.mark.parametrize("mode", (0o640, 0o604, 0o666, 0o000, 0o700))
def test_posix_custody_requires_exact_owner_readable_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: int,
) -> None:
    keyring_path, _control_path = _configure(monkeypatch, tmp_path / "external")
    keyring_path.chmod(mode)

    with pytest.raises(authorization_custody.AuthorizationCustodyUnavailable):
        authorization_custody.load_external_custody(tmp_path / "vault")


@pytest.mark.skipif(os.name == "nt", reason="POSIX hard-link contract")
def test_hard_linked_custody_file_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keyring_path, _control_path = _configure(monkeypatch, tmp_path / "external")
    os.link(keyring_path, keyring_path.with_name("keyring-copy.json"))

    with pytest.raises(authorization_custody.AuthorizationCustodyUnavailable):
        authorization_custody.load_external_custody(tmp_path / "vault")


@pytest.mark.skipif(os.name != "nt", reason="Windows DACL contract")
def test_windows_custody_refuses_a_file_admitting_authenticated_users(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import mutation_lock

    keyring_path, _control_path = _configure(monkeypatch, tmp_path / "external")
    sid = mutation_lock._windows_current_user_sid()
    unsafe = mutation_lock._windows_private_dacl_sddl(sid) + "(A;;FA;;;AU)"
    mutation_lock._windows_apply_dacl_sddl(keyring_path, unsafe)
    observed = mutation_lock._windows_dacl_sddl(keyring_path)
    assert not mutation_lock._windows_private_dacl_is_valid(
        observed, sid, directory=False
    )

    with pytest.raises(authorization_custody.AuthorizationCustodyUnavailable):
        authorization_custody.load_external_custody(tmp_path / "vault")


@pytest.mark.skipif(os.name != "posix", reason="POSIX FIFO contract")
def test_fifo_custody_file_refuses_without_blocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keyring_path, _control_path = _configure(monkeypatch, tmp_path / "external")
    keyring_path.unlink()
    os.mkfifo(keyring_path, mode=0o600)
    assert stat.S_ISFIFO(keyring_path.lstat().st_mode)

    with pytest.raises(authorization_custody.AuthorizationCustodyUnavailable):
        authorization_custody.load_external_custody(tmp_path / "vault")
