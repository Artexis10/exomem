"""Pure validation for deployment-bound activation acknowledgement transport."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.x509.oid import ExtendedKeyUsageOID
from cryptography.x509.verification import PolicyBuilder, Store, VerificationError


def _protocol():
    """Load the shared wire-contract module without requiring a package.

    Scripts load this file directly with `spec_from_file_location`, which gives
    it no parent package, so a relative import raises. The sibling file is the
    byte-identical copy of the runtime's protocol module, and the listener's
    DNS name lives there so the client and the issuer cannot spell it
    differently.
    """

    try:
        from . import hosted_activation_ack_protocol  # noqa: PLC0415

        return hosted_activation_ack_protocol
    except ImportError:
        import importlib.util
        import sys

        path = Path(__file__).with_name("hosted_activation_ack_protocol.py")
        spec = importlib.util.spec_from_file_location(
            "exomem_activation_ack_protocol_copy", path
        )
        if spec is None or spec.loader is None:  # pragma: no cover - packaging fault
            raise
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module


_PROTOCOL = _protocol()
SERVICE_NAME = _PROTOCOL.SERVICE_NAME
listener_dns_name = _PROTOCOL.listener_dns_name


ACTIVATION_ACK_PROTOCOL = "exomem.hosted-activation-ack/v1"
MAX_ACTIVATION_ACK_TRUST_BYTES = 262_144
MAX_ACTIVATION_ACK_CA_CERTIFICATES = 16

# The ClusterIP Service the cell's custody sidecar dials. Both the name and the
# fully-qualified form the certificate carries come from the shared protocol
# module, so the client and the issuer cannot spell them differently again.
ACTIVATION_ACK_SERVICE_NAME = SERVICE_NAME
MAX_ACTIVATION_ACK_CERTIFICATE_BYTES = 65_536
# A mis-issued leaf stays bounded; the routine renewal path costs nothing because
# it reuses the CA and touches no cell.
MAX_ACTIVATION_ACK_CERTIFICATE_LIFETIME = timedelta(days=398)
MIN_ACTIVATION_ACK_CERTIFICATE_REMAINING = timedelta(days=14)
# Tests issue their own disposable CA. Marking it in the subject is what stops a
# test certificate from being mistaken for live readiness; the cost of the check
# firing wrongly is one refused issuance with an explicit reason.
ACTIVATION_ACK_TEST_TRUST_MARKER = "exomem-activation-ack-disposable-test-ca"

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DNS_LABEL = re.compile(r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$")
_CERTIFICATE = re.compile(
    r"-----BEGIN CERTIFICATE-----\n(?:[A-Za-z0-9+/=]{1,64}\n)+-----END CERTIFICATE-----\n"
)


def validate_activation_ack_binding(value: object) -> dict[str, str]:
    """Return one exact non-null lock binding or refuse it."""

    if type(value) is not dict or set(value) != {
        "protocol",
        "platformNamespace",
        "trustBundleSha256",
    }:
        raise ValueError("activation acknowledgement binding fields are incomplete or unknown")
    protocol = value.get("protocol")
    namespace = value.get("platformNamespace")
    digest = value.get("trustBundleSha256")
    if protocol != ACTIVATION_ACK_PROTOCOL:
        raise ValueError("activation acknowledgement protocol is unsupported")
    if not isinstance(namespace, str) or not _DNS_LABEL.fullmatch(namespace):
        raise ValueError("activation acknowledgement platform namespace is invalid")
    if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
        raise ValueError("activation acknowledgement trust digest is invalid")
    return {
        "protocol": protocol,
        "platformNamespace": namespace,
        "trustBundleSha256": digest,
    }


def activation_ack_trust_name(binding: object) -> str:
    """Derive the immutable public trust ConfigMap name."""

    validated = validate_activation_ack_binding(binding)
    return "exomem-ack-ca-" + validated["trustBundleSha256"][:40]


def validate_activation_ack_trust_pem(pem: str, expected_sha256: str) -> str:
    """Validate the exact bounded UTF-8 bytes of a CA-only certificate bundle."""

    if not isinstance(pem, str):
        raise ValueError("activation acknowledgement trust bundle is not text")
    try:
        raw = pem.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise ValueError("activation acknowledgement trust bundle is not UTF-8") from error
    if not 1 <= len(raw) <= MAX_ACTIVATION_ACK_TRUST_BYTES:
        raise ValueError("activation acknowledgement trust bundle exceeds its size bound")
    if not isinstance(expected_sha256, str) or not _SHA256.fullmatch(expected_sha256):
        raise ValueError("activation acknowledgement trust digest is invalid")
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError("activation acknowledgement trust bundle digest differs")
    if "PRIVATE KEY" in pem:
        raise ValueError("activation acknowledgement trust bundle contains private-key material")
    parse_activation_ack_trust_certificates(pem)
    return pem


def parse_activation_ack_trust_certificates(pem: str) -> list[x509.Certificate]:
    """Return the bundle's CA certificates, refusing any non-CA or malformed block.

    Deliberately time-free. This runs at chart render and at release verification,
    so an expiry assertion here would turn an expired CA into a cluster that cannot
    render its own charts -- exactly when an operator is trying to roll a new one.
    Time-dependent checks belong at issuance and in deployment preflight, where the
    failure reads "rotate now" rather than "you cannot deploy".
    """

    blocks = _CERTIFICATE.findall(pem)
    if not blocks or len(blocks) > MAX_ACTIVATION_ACK_CA_CERTIFICATES or "".join(blocks) != pem:
        raise ValueError(
            "activation acknowledgement trust bundle is not a bounded certificate bundle"
        )
    certificates: list[x509.Certificate] = []
    for block in blocks:
        try:
            certificate = x509.load_pem_x509_certificate(block.encode("ascii"))
            constraints = certificate.extensions.get_extension_for_class(
                x509.BasicConstraints
            ).value
        except (ValueError, UnicodeEncodeError, x509.ExtensionNotFound) as error:
            raise ValueError(
                "activation acknowledgement trust bundle has an invalid certificate"
            ) from error
        if not constraints.ca:
            raise ValueError(
                "activation acknowledgement trust bundle contains a non-CA certificate"
            )
        certificates.append(certificate)
    return certificates


def activation_ack_server_dns_name(platform_namespace: str) -> str:
    """Derive the single name the listener's certificate may answer to."""

    if not isinstance(platform_namespace, str) or not _DNS_LABEL.fullmatch(platform_namespace):
        raise ValueError("activation acknowledgement platform namespace is invalid")
    return listener_dns_name(platform_namespace)


def _leaf_subject_contains_test_marker(certificates: list[x509.Certificate]) -> bool:
    for certificate in certificates:
        for attribute in certificate.subject:
            if isinstance(attribute.value, str) and ACTIVATION_ACK_TEST_TRUST_MARKER in (
                attribute.value
            ):
                return True
    return False


def validate_activation_ack_server_certificate(
    certificate_pem: str,
    *,
    platform_namespace: str,
    trust_pem: str,
    now: datetime,
    allow_test_trust: bool = False,
) -> dict[str, object]:
    """Validate the listener's leaf against the pinned bundle at a given instant.

    Unlike the bundle validator this is deliberately time-dependent: it is the
    issuance and preflight check, and a refusal here means rotate, not redeploy.
    """

    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("activation acknowledgement validation instant must be timezone-aware")
    now = now.astimezone(UTC)
    if not isinstance(certificate_pem, str):
        raise ValueError("activation acknowledgement server certificate is not text")
    try:
        raw = certificate_pem.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise ValueError("activation acknowledgement server certificate is not UTF-8") from error
    if not 1 <= len(raw) <= MAX_ACTIVATION_ACK_CERTIFICATE_BYTES:
        raise ValueError("activation acknowledgement server certificate exceeds its size bound")
    if "PRIVATE KEY" in certificate_pem:
        raise ValueError(
            "activation acknowledgement server certificate contains private-key material"
        )
    blocks = _CERTIFICATE.findall(certificate_pem)
    if len(blocks) != 1 or blocks[0] != certificate_pem:
        raise ValueError("activation acknowledgement server certificate is not a single leaf")
    try:
        leaf = x509.load_pem_x509_certificate(raw)
    except ValueError as error:
        raise ValueError("activation acknowledgement server certificate is invalid") from error

    authorities = parse_activation_ack_trust_certificates(trust_pem)
    if not allow_test_trust and _leaf_subject_contains_test_marker([leaf, *authorities]):
        raise ValueError(
            "activation acknowledgement server certificate uses a disposable test authority"
        )

    expected = activation_ack_server_dns_name(platform_namespace)
    try:
        constraints = leaf.extensions.get_extension_for_class(x509.BasicConstraints).value
        usage = leaf.extensions.get_extension_for_class(x509.KeyUsage).value
        extended = leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
        # Every general name, not only the DNS ones. Reading DNS names alone
        # let an IP address, a URI or an RFC822 name ride along unexamined, and
        # the listener is supposed to answer to exactly one name.
        names = list(leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value)
        leaf.extensions.get_extension_for_class(x509.AuthorityKeyIdentifier)
    except x509.ExtensionNotFound as error:
        raise ValueError(
            "activation acknowledgement server certificate is missing a required extension"
        ) from error
    if constraints.ca:
        raise ValueError("activation acknowledgement server certificate is a CA certificate")
    if not usage.digital_signature:
        raise ValueError(
            "activation acknowledgement server certificate cannot sign a TLS handshake"
        )
    if ExtendedKeyUsageOID.SERVER_AUTH not in extended:
        raise ValueError("activation acknowledgement server certificate is not a server key")
    if names != [x509.DNSName(expected)]:
        raise ValueError(
            "activation acknowledgement server certificate does not name the listener Service"
        )

    starts = leaf.not_valid_before_utc
    expires = leaf.not_valid_after_utc
    if now < starts:
        raise ValueError("activation acknowledgement server certificate is not yet valid")
    if now >= expires:
        raise ValueError("activation acknowledgement server certificate has expired")
    if expires - starts > MAX_ACTIVATION_ACK_CERTIFICATE_LIFETIME:
        raise ValueError("activation acknowledgement server certificate lifetime is unbounded")
    remaining = expires - now
    if remaining < MIN_ACTIVATION_ACK_CERTIFICATE_REMAINING:
        raise ValueError(
            "activation acknowledgement server certificate is inside its rotation window"
        )

    verifier = (
        PolicyBuilder()
        .store(Store(authorities))
        .time(now)
        .build_server_verifier(x509.DNSName(expected))
    )
    try:
        verifier.verify(leaf, [])
    except VerificationError as error:
        raise ValueError(
            "activation acknowledgement server certificate does not chain to the pinned trust"
        ) from error

    return {
        "dns_name": expected,
        "not_valid_after": expires.isoformat(),
        "remaining_seconds": int(remaining.total_seconds()),
        "serial_number": format(leaf.serial_number, "x"),
    }
