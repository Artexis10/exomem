#!/usr/bin/env python3
"""Generate and atomically hand off one matching Ed25519 signer/verifier pair."""

from __future__ import annotations

import argparse
import base64
import fcntl
import importlib.util
import os
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def _load_handoff():
    path = Path(__file__).with_name("secret_handoff.py")
    spec = importlib.util.spec_from_file_location("exomem_secret_handoff", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("secret handoff module is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _encode(value: bytes) -> bytes:
    return base64.urlsafe_b64encode(value).rstrip(b"=")


def execute_keypair_handoff(
    *,
    matrix_path: Path,
    repository_root: Path,
    version: str,
    sops_bin: str,
    pair_name: str = "provider_recovery",
) -> None:
    handoff = _load_handoff()
    if not handoff._VERSION.fullmatch(version):
        raise handoff.HandoffError("keypair version must be a positive version")
    matrix = handoff.load_matrix(matrix_path)
    if pair_name not in {
        "provider_recovery",
        "capacity_receipt",
        "economics_receipt",
        "rotation_receipt",
    }:
        raise handoff.HandoffError("keypair contract is unsupported")
    signer = matrix.secrets.get(f"{pair_name}_signing_key")
    verifier = matrix.secrets.get(f"{pair_name}_public_key")
    if signer is None or verifier is None:
        raise handoff.HandoffError("keypair contract is unavailable")
    destinations = [*signer.destinations.values(), *verifier.destinations.values()]
    for destination in destinations:
        handoff._assert_version_is_new_and_increasing(
            repository_root=repository_root,
            relative_template=destination.fields["target"],
            version=version,
            replacements={},
            allow_pending=False,
            label="Ed25519 keypair",
        )

    lock_dir = repository_root / "infra" / "secrets"
    lock_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(lock_dir, 0o700)
    lock_path = lock_dir / f".{pair_name.replace('_', '-')}-keypair.lock"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    published: list[Path] = []
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        for destination in destinations:
            target = repository_root / destination.fields["target"].format(version=version)
            if target.exists():
                raise handoff.HandoffError("keypair version already exists")
        private_key = Ed25519PrivateKey.generate()
        private_value = _encode(
            private_key.private_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PrivateFormat.Raw,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
        public_value = (
            private_key.public_key().public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            if pair_name in {"economics_receipt", "rotation_receipt"}
            else _encode(
                private_key.public_key().public_bytes(
                    encoding=serialization.Encoding.Raw,
                    format=serialization.PublicFormat.Raw,
                )
            )
        )
        try:
            for secret_spec, value in ((signer, private_value), (verifier, public_value)):
                for destination in secret_spec.destinations.values():
                    if destination.kind == "sops_k8s_secret":
                        handoff._seal_k8s_secret(
                            destination=destination,
                            secret=value,
                            version=version,
                            repository_root=repository_root,
                            sops_bin=sops_bin,
                        )
                    elif destination.kind == "sops_escrow":
                        handoff._seal_named_document(
                            destination=destination,
                            secret_name=secret_spec.name,
                            secret=value,
                            version=version,
                            repository_root=repository_root,
                            sops_bin=sops_bin,
                        )
                    else:
                        raise handoff.HandoffError("keypair destination is unsupported")
                    published.append(
                        repository_root / destination.fields["target"].format(version=version)
                    )
        except Exception:
            for target in published:
                target.unlink(missing_ok=True)
                handoff._fsync_directory(target.parent)
            raise
        finally:
            private_value = b""
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate and hand off an approved Ed25519 keypair"
    )
    parser.add_argument("--matrix", type=Path, required=True)
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument(
        "--pair",
        choices=(
            "provider-recovery",
            "capacity-receipt",
            "economics-receipt",
            "rotation-receipt",
        ),
        default="provider-recovery",
    )
    parser.add_argument("--sops-bin", default=os.environ.get("SOPS_BIN", "sops"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        execute_keypair_handoff(
            matrix_path=args.matrix,
            repository_root=args.repository_root,
            version=args.version,
            sops_bin=args.sops_bin,
            pair_name=args.pair.replace("-", "_"),
        )
    except Exception:  # noqa: BLE001 - this boundary must suppress secret-bearing diagnostics
        # The handoff boundary is deliberately content-free: neither provider
        # diagnostics nor exception text may echo generated key material.
        print("Ed25519 keypair handoff rejected", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
