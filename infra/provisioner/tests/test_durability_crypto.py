from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from exomem_provisioner.durability_crypto import (
    AesGcmKeyWrapper,
    ArchiveAuthenticationError,
    ChunkedArchiveCipher,
    RecoveryIdentity,
)


def _identity(**overrides: object) -> RecoveryIdentity:
    values: dict[str, object] = {
        "tenant_id": "tenant-durable-alpha",
        "cell_id": "cell-durable-alpha",
        "operation_id": "operation-durable-alpha",
        "fence_generation": 9,
        "archive_sha256": "a" * 64,
        "manifest_sha256": "b" * 64,
        "archive_size": 150_000,
    }
    values.update(overrides)
    return RecoveryIdentity(**values)  # type: ignore[arg-type]


def test_chunked_envelope_round_trip_authenticates_identity_and_keeps_key_external(
    tmp_path: Path,
) -> None:
    source = tmp_path / "portable.tar"
    source.write_bytes((b"portable-canonical-content\n" * 7000)[:150_000])
    identity = _identity(
        archive_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        archive_size=source.stat().st_size,
    )
    encrypted = tmp_path / "recovery.exomem"
    restored = tmp_path / "restored.tar"
    wrapper = AesGcmKeyWrapper.from_secret(b"root-recovery-key-material" * 2)

    receipt = ChunkedArchiveCipher(chunk_size=32 * 1024).encrypt(
        source,
        encrypted,
        identity=identity,
        key_wrapper=wrapper,
    )

    assert receipt.encryption_scheme == "envelope-aes-256-gcm"
    assert receipt.wrapped_data_key not in encrypted.read_bytes().decode("latin1")
    assert receipt.ciphertext_sha256 == hashlib.sha256(encrypted.read_bytes()).hexdigest()
    assert receipt.ciphertext_size == encrypted.stat().st_size
    assert receipt.authenticated_metadata == identity.provider_metadata()

    ChunkedArchiveCipher(chunk_size=32 * 1024).decrypt(
        encrypted,
        restored,
        identity=identity,
        wrapped_data_key=receipt.wrapped_data_key,
        key_wrapper=wrapper,
    )
    assert restored.read_bytes() == source.read_bytes()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tenant_id", "tenant-other"),
        ("cell_id", "cell-other"),
        ("operation_id", "operation-other"),
        ("fence_generation", 10),
        ("archive_sha256", "c" * 64),
        ("manifest_sha256", "d" * 64),
        ("archive_size", 149_999),
    ],
)
def test_decryption_rejects_every_authenticated_identity_change(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    source = tmp_path / "portable.tar"
    source.write_bytes(b"x" * 150_000)
    identity = _identity(
        archive_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        archive_size=source.stat().st_size,
    )
    encrypted = tmp_path / "recovery.exomem"
    wrapper = AesGcmKeyWrapper.from_secret(b"root-recovery-key-material" * 2)
    receipt = ChunkedArchiveCipher().encrypt(
        source,
        encrypted,
        identity=identity,
        key_wrapper=wrapper,
    )
    altered = _identity(**{**identity.as_dict(), field: value})

    with pytest.raises(ArchiveAuthenticationError):
        ChunkedArchiveCipher().decrypt(
            encrypted,
            tmp_path / "restored.tar",
            identity=altered,
            wrapped_data_key=receipt.wrapped_data_key,
            key_wrapper=wrapper,
        )


def test_truncated_ciphertext_never_publishes_partial_plaintext(tmp_path: Path) -> None:
    source = tmp_path / "portable.tar"
    source.write_bytes(b"x" * 200_000)
    identity = _identity(
        archive_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        archive_size=source.stat().st_size,
    )
    encrypted = tmp_path / "recovery.exomem"
    destination = tmp_path / "restored.tar"
    wrapper = AesGcmKeyWrapper.from_secret(b"root-recovery-key-material" * 2)
    receipt = ChunkedArchiveCipher().encrypt(
        source,
        encrypted,
        identity=identity,
        key_wrapper=wrapper,
    )
    encrypted.write_bytes(encrypted.read_bytes()[:-17])

    with pytest.raises(ArchiveAuthenticationError):
        ChunkedArchiveCipher().decrypt(
            encrypted,
            destination,
            identity=identity,
            wrapped_data_key=receipt.wrapped_data_key,
            key_wrapper=wrapper,
        )
    assert not destination.exists()
