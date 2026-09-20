from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from exomem_provisioner.activation_ack_configuration import (
    ACTIVATION_ACK_TEST_TRUST_MARKER,
    MAX_ACTIVATION_ACK_CA_CERTIFICATES,
    MAX_ACTIVATION_ACK_TRUST_BYTES,
    activation_ack_trust_name,
    parse_activation_ack_trust_certificates,
    validate_activation_ack_binding,
    validate_activation_ack_server_certificate,
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


def _issue(
    *,
    now: datetime,
    dns_name: str = "exomem-activation-ack.exomem-platform.svc.cluster.local",
    starts: timedelta = timedelta(minutes=-5),
    expires: timedelta = timedelta(days=90),
    ca_leaf: bool = False,
    with_authority_key_id: bool = True,
    ca_common_name: str = "Exomem activation acknowledgement CA",
    authority: tuple[object, x509.Certificate] | None = None,
) -> tuple[str, str, tuple[object, x509.Certificate]]:
    """Return (leaf PEM, trust bundle PEM, reusable authority)."""

    if authority is None:
        ca_key = ec.generate_private_key(ec.SECP256R1())
        ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, ca_common_name)])
        ca = (
            x509.CertificateBuilder()
            .subject_name(ca_name)
            .issuer_name(ca_name)
            .public_key(ca_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            # X.509 path validation requires key usage on the authority itself;
            # omitting it fails the chain with a bare "missing required extension"
            # against OID 2.5.29.15, which reads as a leaf problem and is not.
            .add_extension(
                x509.KeyUsage(
                    digital_signature=False,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=True,
                    crl_sign=True,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False
            )
            .sign(ca_key, hashes.SHA256())
        )
        authority = (ca_key, ca)
    ca_key, ca = authority

    leaf_key = ec.generate_private_key(ec.SECP256R1())
    builder = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, dns_name)]))
        .issuer_name(ca.subject)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now + starts)
        .not_valid_after(now + expires)
        .add_extension(x509.BasicConstraints(ca=ca_leaf, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(dns_name)]), critical=False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=ca_leaf,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(leaf_key.public_key()), critical=False
        )
    )
    if with_authority_key_id:
        builder = builder.add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
    leaf = builder.sign(ca_key, hashes.SHA256())
    return (
        leaf.public_bytes(serialization.Encoding.PEM).decode("ascii"),
        ca.public_bytes(serialization.Encoding.PEM).decode("ascii"),
        authority,
    )


def test_server_certificate_names_the_listener_service_and_reports_its_window() -> None:
    now = datetime.now(UTC)
    leaf, trust, _ = _issue(now=now)

    report = validate_activation_ack_server_certificate(
        leaf, platform_namespace="exomem-platform", trust_pem=trust, now=now
    )

    assert report["dns_name"] == "exomem-activation-ack.exomem-platform.svc.cluster.local"
    assert 88 * 86400 < report["remaining_seconds"] <= 90 * 86400
    assert report["not_valid_after"].endswith("+00:00")


def test_server_certificate_refuses_every_way_it_could_be_the_wrong_certificate() -> None:
    now = datetime.now(UTC)
    good_leaf, good_trust, authority = _issue(now=now)

    wrong_name, wrong_trust, _ = _issue(now=now, dns_name="exomem-activation-ack.other.svc")
    expired, expired_trust, _ = _issue(
        now=now, starts=timedelta(days=-400), expires=timedelta(days=-1)
    )
    future, future_trust, _ = _issue(now=now, starts=timedelta(days=1), expires=timedelta(days=90))
    too_long, too_long_trust, _ = _issue(now=now, expires=timedelta(days=400))
    rotating, rotating_trust, _ = _issue(now=now, expires=timedelta(days=13))
    ca_leaf, ca_leaf_trust, _ = _issue(now=now, ca_leaf=True)
    no_akid, no_akid_trust, _ = _issue(now=now, with_authority_key_id=False)
    _, foreign_trust, _ = _issue(now=now)

    for certificate, trust in (
        (wrong_name, wrong_trust),
        (expired, expired_trust),
        (future, future_trust),
        (too_long, too_long_trust),
        (rotating, rotating_trust),
        (ca_leaf, ca_leaf_trust),
        (no_akid, no_akid_trust),
        (good_leaf, foreign_trust),
        (good_leaf + good_leaf, good_trust),
    ):
        with pytest.raises(ValueError):
            validate_activation_ack_server_certificate(
                certificate, platform_namespace="exomem-platform", trust_pem=trust, now=now
            )

    with pytest.raises(ValueError):
        validate_activation_ack_server_certificate(
            good_leaf,
            platform_namespace="exomem-platform",
            trust_pem=good_trust,
            now=now.replace(tzinfo=None),
        )

    # The same authority still issues an acceptable certificate, so the refusals
    # above are about each certificate rather than about the fixture.
    renewed, _, _ = _issue(now=now, authority=authority)
    assert validate_activation_ack_server_certificate(
        renewed, platform_namespace="exomem-platform", trust_pem=good_trust, now=now
    )


def test_a_disposable_test_authority_is_never_live_readiness() -> None:
    now = datetime.now(UTC)
    leaf, trust, _ = _issue(now=now, ca_common_name=ACTIVATION_ACK_TEST_TRUST_MARKER)

    with pytest.raises(ValueError):
        validate_activation_ack_server_certificate(
            leaf, platform_namespace="exomem-platform", trust_pem=trust, now=now
        )

    assert validate_activation_ack_server_certificate(
        leaf,
        platform_namespace="exomem-platform",
        trust_pem=trust,
        now=now,
        allow_test_trust=True,
    )


def test_trust_bundle_validation_stays_time_free_so_an_expired_ca_still_renders() -> None:
    now = datetime.now(UTC)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Exomem expired CA")])
    expired_ca = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=800))
        .not_valid_after(now - timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    pem = expired_ca.public_bytes(serialization.Encoding.PEM).decode("ascii")

    # An expired CA must not make the chart unrenderable; rotation is the remedy
    # and it needs a cluster that can still render.
    assert validate_activation_ack_trust_pem(pem, hashlib.sha256(pem.encode()).hexdigest()) == pem
    assert len(parse_activation_ack_trust_certificates(pem)) == 1
