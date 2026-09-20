from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from exomem_provisioner.activation_ack_configuration import (
    MAX_ACTIVATION_ACK_CA_CERTIFICATES,
    MAX_ACTIVATION_ACK_TRUST_BYTES,
    activation_ack_trust_name,
    validate_activation_ack_binding,
    validate_activation_ack_trust_pem,
)


def _ca_pem(*, ca: bool = True) -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(UTC)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Exomem test CA")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return certificate.public_bytes(serialization.Encoding.PEM).decode("ascii")


def test_activation_ack_binding_is_closed_and_derives_full_digest_name() -> None:
    binding = {
        "protocol": "exomem.hosted-activation-ack/v1",
        "platformNamespace": "exomem-platform",
        "trustBundleSha256": "a" * 64,
    }

    assert validate_activation_ack_binding(binding) == binding
    assert activation_ack_trust_name(binding) == "exomem-ack-ca-" + "a" * 40

    for invalid in (
        None,
        {**binding, "extra": True},
        {key: value for key, value in binding.items() if key != "protocol"},
        {**binding, "protocol": "v2"},
        {**binding, "platformNamespace": "Not-DNS"},
        {**binding, "trustBundleSha256": "a" * 63},
    ):
        with pytest.raises(ValueError):
            validate_activation_ack_binding(invalid)


def test_activation_ack_trust_pem_is_exact_bounded_ca_bundle() -> None:
    pem = _ca_pem()
    digest = hashlib.sha256(pem.encode("utf-8")).hexdigest()

    assert validate_activation_ack_trust_pem(pem, digest) == pem
    assert MAX_ACTIVATION_ACK_TRUST_BYTES == 262_144
    assert MAX_ACTIVATION_ACK_CA_CERTIFICATES == 16

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")
    non_ca_pem = _ca_pem(ca=False)
    invalid = (
        ("", hashlib.sha256(b"").hexdigest()),
        (pem, "b" * 64),
        (pem.rstrip("\n"), hashlib.sha256(pem.rstrip("\n").encode()).hexdigest()),
        (private_pem, hashlib.sha256(private_pem.encode()).hexdigest()),
        (non_ca_pem, hashlib.sha256(non_ca_pem.encode()).hexdigest()),
        (pem * 17, hashlib.sha256((pem * 17).encode()).hexdigest()),
        (
            "x" * (MAX_ACTIVATION_ACK_TRUST_BYTES + 1),
            hashlib.sha256(("x" * (MAX_ACTIVATION_ACK_TRUST_BYTES + 1)).encode()).hexdigest(),
        ),
    )
    for value, expected in invalid:
        with pytest.raises(ValueError):
            validate_activation_ack_trust_pem(value, expected)
