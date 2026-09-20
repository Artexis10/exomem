#!/usr/bin/env python3
"""Issue and atomically hand off the activation acknowledgement listener certificate.

Two modes, because rotation has two tiers and only one of them is expensive.

`--new-authority` mints a certificate authority and a leaf under it. The CA's
public certificate becomes the trust bundle, whose digest the deployment lock
pins and whose bytes every cell projects into an immutable ConfigMap, so a new
authority means a new lock and a cell-by-cell rollout.

The default renews the leaf under an authority that already exists, read back
from its escrow artifact. The trust bundle does not change, so the digest, the
lock and every cell stay exactly as they are. This is the routine answer to an
expiring certificate.

Neither mode prints key material, and the failure boundary is content-free.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

_TLS_SECRET = "activation_ack_tls_pair"
_CA_SECRET = "activation_ack_ca_private_key"
_TLS_DESTINATION = "k3s.activation-ack-tls.active"
_CA_DESTINATION = "escrow.activation-ack-ca.active"


class CertificateHandoffError(RuntimeError):
    """Refusal raised by this command. Never carries key material."""


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise CertificateHandoffError(f"{name} is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_handoff():
    return _load("exomem_secret_handoff", Path(__file__).with_name("secret_handoff.py"))


def _load_activation_ack_configuration():
    # Part of the tool, not of the tree being published into: a handoff may run
    # against a repository root that is only a destination.
    return _load(
        "activation_ack_configuration",
        Path(__file__).resolve().parents[1]
        / "provisioner/src/exomem_provisioner/activation_ack_configuration.py",
    )


def _pem(certificate: x509.Certificate) -> bytes:
    return certificate.public_bytes(serialization.Encoding.PEM)


def _private_pem(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def _key_usage(*, certificate_authority: bool) -> x509.KeyUsage:
    return x509.KeyUsage(
        digital_signature=not certificate_authority,
        content_commitment=False,
        key_encipherment=not certificate_authority,
        data_encipherment=False,
        key_agreement=False,
        key_cert_sign=certificate_authority,
        crl_sign=certificate_authority,
        encipher_only=False,
        decipher_only=False,
    )


def build_authority(
    *,
    now: dt.datetime,
    lifetime_days: int,
    common_name: str,
) -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate]:
    """Mint the internal authority that signs the listener's certificate."""

    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=lifetime_days))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        # Path validation refuses a chain whose authority carries no key usage,
        # and reports it against the leaf. Both this and the leaf's authority key
        # identifier below are required for the library verification path.
        .add_extension(_key_usage(certificate_authority=True), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    return key, certificate


def build_listener_certificate(
    *,
    authority_key: ec.EllipticCurvePrivateKey,
    authority: x509.Certificate,
    dns_name: str,
    now: dt.datetime,
    lifetime_days: int,
) -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate]:
    """Issue the single-name server certificate the cell's sidecar will accept."""

    key = ec.generate_private_key(ec.SECP256R1())
    certificate = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, dns_name)]))
        .issuer_name(authority.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=lifetime_days))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(dns_name)]), critical=False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(_key_usage(certificate_authority=False), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(authority.public_key()),
            critical=False,
        )
        .sign(authority_key, hashes.SHA256())
    )
    return key, certificate


def _read_escrowed_authority(*, artifact: Path, sops_bin: str) -> ec.EllipticCurvePrivateKey:
    """Recover the authority's private key from its escrow artifact, in memory."""

    try:
        result = subprocess.run(
            [sops_bin, "decrypt", "--input-type", "json", "--output-type", "json", str(artifact)],
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        # `TimeoutExpired.stdout` holds the partially decrypted document, so the
        # chain is severed rather than attached: this boundary is content-free,
        # and a structured logger that serializes `__cause__` would leak it.
        raise CertificateHandoffError("authority decrypt failed") from None
    if result.returncode != 0:
        raise CertificateHandoffError("authority decrypt failed")
    try:
        document = json.loads(result.stdout)
        material = document["ca_private_key"]
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError):
        # `UnicodeDecodeError.object` is the whole plaintext byte string.
        raise CertificateHandoffError("escrowed authority is malformed") from None
    if not isinstance(material, str):
        raise CertificateHandoffError("escrowed authority is malformed")
    try:
        key = serialization.load_pem_private_key(material.encode("utf-8"), password=None)
    except ValueError as error:
        raise CertificateHandoffError("escrowed authority is malformed") from error
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise CertificateHandoffError("escrowed authority is not an elliptic-curve key")
    return key


def _matching_authority(
    *, key: ec.EllipticCurvePrivateKey, trust_pem: str, configuration
) -> x509.Certificate:
    """Pick the bundle certificate this private key actually signs for."""

    authorities = configuration.parse_activation_ack_trust_certificates(trust_pem)
    public_numbers = key.public_key().public_numbers()
    for candidate in authorities:
        candidate_public = candidate.public_key()
        if (
            isinstance(candidate_public, ec.EllipticCurvePublicKey)
            and candidate_public.public_numbers() == public_numbers
        ):
            return candidate
    raise CertificateHandoffError("escrowed authority does not appear in the trust bundle")


def execute_certificate_handoff(
    *,
    matrix_path: Path,
    repository_root: Path,
    version: str,
    platform_namespace: str,
    trust_bundle_path: Path,
    sops_bin: str,
    new_authority: bool,
    authority_artifact: Path | None,
    certificate_lifetime_days: int,
    authority_lifetime_days: int,
    test_authority: bool,
    now: dt.datetime | None = None,
) -> dict[str, object]:
    handoff = _load_handoff()
    configuration = _load_activation_ack_configuration()
    now = dt.datetime.now(dt.UTC) if now is None else now

    if not handoff._VERSION.fullmatch(version):
        raise CertificateHandoffError("certificate version must be a positive version")
    if new_authority == (authority_artifact is not None):
        raise CertificateHandoffError(
            "choose exactly one of a new authority or an existing escrow artifact"
        )
    dns_name = configuration.activation_ack_server_dns_name(platform_namespace)

    matrix = handoff.load_matrix(matrix_path)
    pair = matrix.secrets.get(_TLS_SECRET)
    authority_secret = matrix.secrets.get(_CA_SECRET)
    if pair is None or authority_secret is None:
        raise CertificateHandoffError("activation acknowledgement certificate route is unavailable")
    tls_destination = pair.destinations.get(_TLS_DESTINATION)
    escrow_destination = authority_secret.destinations.get(_CA_DESTINATION)
    if tls_destination is None or escrow_destination is None:
        raise CertificateHandoffError("activation acknowledgement certificate route is unavailable")

    published: list[Path] = []
    lock_dir = repository_root / "infra" / "secrets"
    lock_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(lock_dir, 0o700)
    lock_path = lock_dir / ".activation-ack-certificate.lock"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        targets = [tls_destination]
        if new_authority:
            targets.append(escrow_destination)
        for destination in targets:
            handoff._assert_version_is_new_and_increasing(
                repository_root=repository_root,
                relative_template=destination.fields["target"],
                version=version,
                replacements={},
                allow_pending=False,
                label="activation acknowledgement certificate",
            )

        if new_authority:
            common_name = (
                configuration.ACTIVATION_ACK_TEST_TRUST_MARKER
                if test_authority
                else "Exomem activation acknowledgement CA"
            )
            authority_key, authority = build_authority(
                now=now, lifetime_days=authority_lifetime_days, common_name=common_name
            )
            trust_pem = _pem(authority).decode("ascii")
        else:
            assert authority_artifact is not None
            if not trust_bundle_path.is_file():
                raise CertificateHandoffError("renewal requires the existing trust bundle")
            trust_pem = trust_bundle_path.read_text(encoding="utf-8")
            authority_key = _read_escrowed_authority(artifact=authority_artifact, sops_bin=sops_bin)
            authority = _matching_authority(
                key=authority_key, trust_pem=trust_pem, configuration=configuration
            )

        listener_key, listener = build_listener_certificate(
            authority_key=authority_key,
            authority=authority,
            dns_name=dns_name,
            now=now,
            lifetime_days=certificate_lifetime_days,
        )

        # Validate before publishing anything. A certificate that would be
        # refused at preflight must never reach an artifact, because a sealed
        # version number is spent whether or not the value was usable.
        report = configuration.validate_activation_ack_server_certificate(
            _pem(listener).decode("ascii"),
            platform_namespace=platform_namespace,
            trust_pem=trust_pem,
            now=now,
            allow_test_trust=test_authority,
        )

        try:
            handoff.seal_k8s_tls_secret(
                destination=tls_destination,
                certificate=_pem(listener),
                private_key=_private_pem(listener_key),
                version=version,
                repository_root=repository_root,
                sops_bin=sops_bin,
            )
            published.append(
                repository_root / tls_destination.fields["target"].format(version=version)
            )
            if new_authority:
                handoff._seal_named_document(
                    destination=escrow_destination,
                    secret_name=_CA_SECRET,
                    secret=_private_pem(authority_key),
                    version=version,
                    repository_root=repository_root,
                    sops_bin=sops_bin,
                )
                published.append(
                    repository_root / escrow_destination.fields["target"].format(version=version)
                )
                trust_bundle_path.parent.mkdir(parents=True, exist_ok=True)
                trust_bundle_path.write_text(trust_pem, encoding="utf-8")
                trust_bundle_path.chmod(0o644)
        except BaseException:
            for path in published:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            raise
    finally:
        os.close(descriptor)

    return {
        "dns_name": report["dns_name"],
        "not_valid_after": report["not_valid_after"],
        "serial_number": report["serial_number"],
        "trust_bundle_sha256": hashlib.sha256(trust_pem.encode("utf-8")).hexdigest(),
        "rotated_authority": new_authority,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", type=Path, required=True)
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--platform-namespace", required=True)
    parser.add_argument(
        "--trust-bundle",
        type=Path,
        required=True,
        help="CA-only PEM: written on a new authority, read on a renewal.",
    )
    parser.add_argument("--new-authority", action="store_true")
    parser.add_argument(
        "--authority",
        type=Path,
        default=None,
        help="Escrow artifact holding the existing authority, for a renewal.",
    )
    parser.add_argument("--certificate-lifetime-days", type=int, default=90)
    parser.add_argument("--authority-lifetime-days", type=int, default=3650)
    parser.add_argument(
        "--test-authority",
        action="store_true",
        help="Mark the authority disposable. Preflight refuses it as live readiness.",
    )
    parser.add_argument("--sops-bin", default="sops")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = execute_certificate_handoff(
            matrix_path=args.matrix,
            repository_root=args.repository_root,
            version=args.version,
            platform_namespace=args.platform_namespace,
            trust_bundle_path=args.trust_bundle,
            sops_bin=args.sops_bin,
            new_authority=args.new_authority,
            authority_artifact=args.authority,
            certificate_lifetime_days=args.certificate_lifetime_days,
            authority_lifetime_days=args.authority_lifetime_days,
            test_authority=args.test_authority,
        )
    except Exception:  # noqa: BLE001 - this boundary must suppress secret-bearing diagnostics
        # Content-free like the keypair handoff: neither provider diagnostics nor
        # exception text may echo generated key material.
        print("activation acknowledgement certificate handoff rejected", file=sys.stderr)
        return 2
    # Everything printed here is public: a DNS name, an expiry, a serial and the
    # digest the deployment lock will pin.
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
