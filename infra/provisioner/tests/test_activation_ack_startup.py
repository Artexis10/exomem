"""The worker refuses to answer on a certificate the cells could not verify.

Every other test in this repair checks a certificate at the moment it is
issued. This one checks the certificate the worker actually loads, which is a
different thing: a Secret can be replaced, rolled back or left to expire long
after the issuer last saw it.

The interesting case is the one that does *not* refuse. A certificate inside
its rotation window is valid, and refusing to serve it would strand every
capable cell now to prevent something days away.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import logging
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization

from exomem_provisioner.activation_ack_configuration import (
    ACTIVATION_ACK_PROTOCOL,
    ACTIVATION_ACK_TEST_TRUST_MARKER,
    activation_ack_server_dns_name,
)
from exomem_provisioner.activation_ack_startup import (
    ActivationAckStartupRefusal,
    preflight_activation_ack_listener,
)
from exomem_provisioner.config import ActivationAcknowledgementBinding

NAMESPACE = "exomem-platform"
ROOT = Path(__file__).resolve().parents[3]
DAY = dt.timedelta(days=1)


def _issuer():
    path = ROOT / "infra/scripts/activation_ack_certificate_handoff.py"
    spec = importlib.util.spec_from_file_location("activation_ack_certificate_handoff", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _pem(certificate) -> str:
    return certificate.public_bytes(serialization.Encoding.PEM).decode("ascii")


def _material(
    tmp_path: Path,
    *,
    issued_at: dt.datetime,
    lifetime_days: int = 90,
    dns_name: str | None = None,
    authority_common_name: str = "Exomem activation acknowledgement CA",
) -> tuple[ActivationAcknowledgementBinding, str, Path]:
    """Mint a CA and a leaf, and return the lock binding, bundle and cert path."""

    issuer = _issuer()
    authority_key, authority = issuer.build_authority(
        now=issued_at, lifetime_days=3650, common_name=authority_common_name
    )
    _, leaf = issuer.build_listener_certificate(
        authority_key=authority_key,
        authority=authority,
        dns_name=dns_name or activation_ack_server_dns_name(NAMESPACE),
        now=issued_at,
        lifetime_days=lifetime_days,
    )
    trust_pem = _pem(authority)
    binding = ActivationAcknowledgementBinding(
        protocol=ACTIVATION_ACK_PROTOCOL,
        platformNamespace=NAMESPACE,
        trustBundleSha256=hashlib.sha256(trust_pem.encode("utf-8")).hexdigest(),
    )
    tmp_path.mkdir(parents=True, exist_ok=True)
    certificate_path = tmp_path / "tls.crt"
    certificate_path.write_text(_pem(leaf), encoding="utf-8")
    return binding, trust_pem, certificate_path


def _reader(trust_pem: str):
    """Mimic the real adapter: the digest in the lock decides what is returned."""

    async def read(binding: dict[str, str]) -> str:
        digest = hashlib.sha256(trust_pem.encode("utf-8")).hexdigest()
        if binding["trustBundleSha256"] != digest:
            raise RuntimeError("activation acknowledgement trust bundle digest differs")
        return trust_pem

    return read


async def test_the_certificate_the_worker_loads_is_checked_against_the_pinned_bundle(
    tmp_path: Path,
) -> None:
    now = dt.datetime.now(dt.UTC).replace(microsecond=0)
    binding, trust_pem, certificate_path = _material(tmp_path, issued_at=now)

    report = await preflight_activation_ack_listener(
        binding=binding,
        certificate_path=str(certificate_path),
        read_trust_bundle=_reader(trust_pem),
        now=now,
    )

    assert report["dns_name"] == activation_ack_server_dns_name(NAMESPACE)
    assert report["remaining_seconds"] == int((90 * DAY).total_seconds())


async def test_a_certificate_inside_its_rotation_window_still_serves_and_says_so(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The control that is deliberately not a refusal.

    Five days of validity left is a certificate that works. Refusing it would
    take the fleet down now to avoid a handshake failure five days away, and
    the cells it would strand cannot be recovered afterwards. The worker serves
    and makes the rotation impossible to miss instead.
    """

    now = dt.datetime.now(dt.UTC).replace(microsecond=0)
    binding, trust_pem, certificate_path = _material(tmp_path, issued_at=now - 85 * DAY)

    with caplog.at_level(logging.WARNING, logger="exomem_provisioner.activation_ack_startup"):
        report = await preflight_activation_ack_listener(
            binding=binding,
            certificate_path=str(certificate_path),
            read_trust_bundle=_reader(trust_pem),
            now=now,
        )

    assert report["remaining_seconds"] == int((5 * DAY).total_seconds())
    warnings = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "activation-ack-certificate-rotation-due"
    ]
    assert len(warnings) == 1
    assert warnings[0].levelno == logging.WARNING
    assert warnings[0].state == "5d-remaining"


async def test_an_expired_certificate_is_refused(tmp_path: Path) -> None:
    now = dt.datetime.now(dt.UTC).replace(microsecond=0)
    binding, trust_pem, certificate_path = _material(tmp_path, issued_at=now - 120 * DAY)

    with pytest.raises(ActivationAckStartupRefusal, match="not the one the lock pins"):
        await preflight_activation_ack_listener(
            binding=binding,
            certificate_path=str(certificate_path),
            read_trust_bundle=_reader(trust_pem),
            now=now,
        )


async def test_a_certificate_for_another_namespace_is_refused(tmp_path: Path) -> None:
    """This is the mis-issuance that produced a fleet-wide handshake failure."""

    now = dt.datetime.now(dt.UTC).replace(microsecond=0)
    binding, trust_pem, certificate_path = _material(
        tmp_path,
        issued_at=now,
        dns_name=activation_ack_server_dns_name("exomem-other"),
    )

    with pytest.raises(ActivationAckStartupRefusal, match="not the one the lock pins"):
        await preflight_activation_ack_listener(
            binding=binding,
            certificate_path=str(certificate_path),
            read_trust_bundle=_reader(trust_pem),
            now=now,
        )


async def test_a_certificate_from_a_different_authority_is_refused(tmp_path: Path) -> None:
    """A hand-replaced Secret is exactly the case the issuer cannot see."""

    now = dt.datetime.now(dt.UTC).replace(microsecond=0)
    binding, trust_pem, _ = _material(tmp_path, issued_at=now)
    _, _, foreign_path = _material(tmp_path / "foreign", issued_at=now)

    with pytest.raises(ActivationAckStartupRefusal, match="not the one the lock pins"):
        await preflight_activation_ack_listener(
            binding=binding,
            certificate_path=str(foreign_path),
            read_trust_bundle=_reader(trust_pem),
            now=now,
        )


async def test_a_disposable_test_authority_never_serves_live(tmp_path: Path) -> None:
    now = dt.datetime.now(dt.UTC).replace(microsecond=0)
    binding, trust_pem, certificate_path = _material(
        tmp_path,
        issued_at=now,
        authority_common_name=ACTIVATION_ACK_TEST_TRUST_MARKER,
    )

    with pytest.raises(ActivationAckStartupRefusal, match="not the one the lock pins"):
        await preflight_activation_ack_listener(
            binding=binding,
            certificate_path=str(certificate_path),
            read_trust_bundle=_reader(trust_pem),
            now=now,
        )

    # The same material is admissible when a caller says it is a drill.
    report = await preflight_activation_ack_listener(
        binding=binding,
        certificate_path=str(certificate_path),
        read_trust_bundle=_reader(trust_pem),
        now=now,
        allow_test_trust=True,
    )
    assert report["dns_name"] == activation_ack_server_dns_name(NAMESPACE)


async def test_a_listener_configured_without_a_lock_binding_is_refused(tmp_path: Path) -> None:
    """The environment says serve; the lock binds no protocol. Serving anyway
    would put a listener in front of cells that hold no trust bundle for it."""

    now = dt.datetime.now(dt.UTC).replace(microsecond=0)
    _, trust_pem, certificate_path = _material(tmp_path, issued_at=now)

    with pytest.raises(ActivationAckStartupRefusal, match="without a lock binding"):
        await preflight_activation_ack_listener(
            binding=None,
            certificate_path=str(certificate_path),
            read_trust_bundle=_reader(trust_pem),
            now=now,
        )


async def test_an_unreadable_certificate_mount_is_refused(tmp_path: Path) -> None:
    now = dt.datetime.now(dt.UTC).replace(microsecond=0)
    binding, trust_pem, certificate_path = _material(tmp_path, issued_at=now)

    with pytest.raises(ActivationAckStartupRefusal, match="unreadable"):
        await preflight_activation_ack_listener(
            binding=binding,
            certificate_path=str(certificate_path.with_name("absent.crt")),
            read_trust_bundle=_reader(trust_pem),
            now=now,
        )

    link = tmp_path / "linked.crt"
    link.symlink_to(certificate_path)
    with pytest.raises(ActivationAckStartupRefusal, match="symlink"):
        await preflight_activation_ack_listener(
            binding=binding,
            certificate_path=str(link),
            read_trust_bundle=_reader(trust_pem),
            now=now,
        )


async def test_an_unavailable_trust_bundle_is_refused(tmp_path: Path) -> None:
    """The lock's digest is what decides; a rotated ConfigMap is not a fallback."""

    now = dt.datetime.now(dt.UTC).replace(microsecond=0)
    binding, _, certificate_path = _material(tmp_path, issued_at=now)
    _, other_trust, _ = _material(tmp_path / "other", issued_at=now)

    with pytest.raises(ActivationAckStartupRefusal, match="trust bundle is unavailable"):
        await preflight_activation_ack_listener(
            binding=binding,
            certificate_path=str(certificate_path),
            read_trust_bundle=_reader(other_trust),
            now=now,
        )


async def test_a_kubernetes_secret_projection_layout_refuses_and_a_subpath_mount_does_not(
    tmp_path: Path,
) -> None:
    """Why the chart mounts each key by subPath instead of the whole directory.

    kubelet writes a Secret volume as an atomic-swap tree: the real bytes live
    under a timestamped directory, `..data` points at it, and every key is a
    symlink into `..data/`. A directory mount therefore hands this preflight a
    symlink, it refuses, and the refusal takes the whole worker down rather than
    just the listener. The same shape mounted by subPath is a regular file.

    This is the deployment half of the plain-symlink refusal above; the chart
    half is asserted in `tests/test_hosted_activation_helm.py`.
    """

    now = dt.datetime.now(dt.UTC).replace(microsecond=0)
    binding, trust_pem, certificate_path = _material(tmp_path, issued_at=now)

    mount = tmp_path / "projection"
    revision = mount / "..2026_09_21_00_00_00.000000000"
    revision.mkdir(parents=True)
    (revision / "tls.crt").write_bytes(certificate_path.read_bytes())
    (mount / "..data").symlink_to(revision.name)
    (mount / "tls.crt").symlink_to(Path("..data") / "tls.crt")

    with pytest.raises(ActivationAckStartupRefusal, match="symlink"):
        await preflight_activation_ack_listener(
            binding=binding,
            certificate_path=str(mount / "tls.crt"),
            read_trust_bundle=_reader(trust_pem),
            now=now,
        )

    # What the chart actually mounts: kubelet bind-mounts the key as a regular
    # file at the mount path, so the same projected bytes pass.
    materialized = tmp_path / "subpath-tls.crt"
    materialized.write_bytes((revision / "tls.crt").read_bytes())
    report = await preflight_activation_ack_listener(
        binding=binding,
        certificate_path=str(materialized),
        read_trust_bundle=_reader(trust_pem),
        now=now,
    )
    assert report["dns_name"] == activation_ack_server_dns_name(NAMESPACE)
