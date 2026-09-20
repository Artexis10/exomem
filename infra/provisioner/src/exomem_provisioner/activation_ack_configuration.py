"""Pure validation for deployment-bound activation acknowledgement transport."""

from __future__ import annotations

import hashlib
import re

from cryptography import x509

ACTIVATION_ACK_PROTOCOL = "exomem.hosted-activation-ack/v1"
MAX_ACTIVATION_ACK_TRUST_BYTES = 262_144
MAX_ACTIVATION_ACK_CA_CERTIFICATES = 16

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
    blocks = _CERTIFICATE.findall(pem)
    if not blocks or len(blocks) > MAX_ACTIVATION_ACK_CA_CERTIFICATES or "".join(blocks) != pem:
        raise ValueError(
            "activation acknowledgement trust bundle is not a bounded certificate bundle"
        )
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
    return pem
