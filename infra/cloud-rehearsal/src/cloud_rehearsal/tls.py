"""A throwaway certificate authority for the rehearsal's two public names.

Production terminates TLS with ACME certificates (D11 for the MCP hostname,
Vercel for Substrate). The rehearsal has no public DNS, so one CA made per
run signs both leaf certificates, and every client in the run trusts only
that CA. The private keys live in the run's work directory and die with it.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

SUBSTRATE_HOST = "substrate.rehearsal.test"
MCP_HOST = "mcp.rehearsal.test"


@dataclass(frozen=True)
class LeafCertificate:
    cert_pem: str
    key_pem: str


@dataclass(frozen=True)
class RehearsalPki:
    ca_path: Path
    leaves: dict[str, LeafCertificate]


def _key_pem(key: ec.EllipticCurvePrivateKey) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


def make_pki(workdir: Path, hostnames: tuple[str, ...] = (SUBSTRATE_HOST, MCP_HOST)) -> RehearsalPki:
    now = dt.datetime.now(dt.UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Exomem Cloud rehearsal CA")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=2))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        # Python 3.13 verifies strictly (VERIFY_X509_STRICT): key identifiers
        # on the CA and the leaves are required, as a public CA issues them.
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, key_cert_sign=True, crl_sign=True, content_commitment=False,
                key_encipherment=False, data_encipherment=False, key_agreement=False,
                encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )
    leaves: dict[str, LeafCertificate] = {}
    for hostname in hostnames:
        key = ec.generate_private_key(ec.SECP256R1())
        cert = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)]))
            .issuer_name(ca_name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False)
            .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False
            )
            .sign(ca_key, hashes.SHA256())
        )
        leaves[hostname] = LeafCertificate(
            cert_pem=cert.public_bytes(serialization.Encoding.PEM).decode(), key_pem=_key_pem(key)
        )
    tls_dir = workdir / "tls"
    tls_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    ca_path = tls_dir / "ca.pem"
    ca_path.write_text(ca_cert.public_bytes(serialization.Encoding.PEM).decode(), encoding="utf-8")
    return RehearsalPki(ca_path=ca_path, leaves=leaves)
