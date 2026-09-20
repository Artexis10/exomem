"""Prove the listener's certificate before the worker answers on it.

The issuance path validates a certificate when it mints one, but nothing read
it again afterwards. A Secret can be replaced by hand, rolled back to an older
pair, or simply left alone until it expires, and none of that touches the
issuer. So the certificate the worker actually loads had never been checked
against the trust bundle its own deployment lock pins.

That gap is worse here than it looks. A cell whose handshake fails cannot
acknowledge its activation tuple; a cell that cannot acknowledge cannot sign
its readiness attestation; an attestation that is not renewed lapses within the
hour, and minting is fenced off for a cell that has already served. A wrong
certificate is therefore not a degraded listener -- it is a silent, fleet-wide,
irreversible loss that begins the moment the first governed write lands.

What this refusal prevents, what it costs when it fires wrongly, and who pays:
it prevents that loss; it costs a worker that will not start, so routine
lifecycle operations pause until the certificate Secret is fixed or rolled
back; and the operator pays, at deploy time, with the reason in the first log
line and no cell yet harmed. A certificate that does not chain to the bundle
pinned in its own lock is a fault, not an eventually-consistent state, so
fail-closed is the right category.

The rotation floor is deliberately *not* fatal. Refusing to serve because a
valid certificate has nine days left would destroy the fleet now to prevent
something that happens in nine days -- the wrong-firing cost far exceeds what
it prevents. It is logged instead, loudly, every time the worker starts.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .activation_ack_configuration import (
    MAX_ACTIVATION_ACK_CERTIFICATE_BYTES,
    MIN_ACTIVATION_ACK_CERTIFICATE_REMAINING,
    validate_activation_ack_server_certificate,
)
from .config import ActivationAcknowledgementBinding

_LOG = logging.getLogger(__name__)


class ActivationAckStartupRefusal(RuntimeError):
    """The worker must not answer on this listener."""


def _read_certificate(path: str) -> str:
    certificate = Path(path)
    try:
        if certificate.is_symlink():
            raise ActivationAckStartupRefusal(
                "activation acknowledgement server certificate is a symlink"
            )
        # Bounded before decoding: the mount is a Secret projection, and a
        # certificate that does not fit the issuance bound is not one we wrote.
        raw = certificate.read_bytes()
    except OSError as error:
        raise ActivationAckStartupRefusal(
            "activation acknowledgement server certificate is unreadable"
        ) from error
    if not 1 <= len(raw) <= MAX_ACTIVATION_ACK_CERTIFICATE_BYTES:
        raise ActivationAckStartupRefusal(
            "activation acknowledgement server certificate exceeds its size bound"
        )
    try:
        return raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise ActivationAckStartupRefusal(
            "activation acknowledgement server certificate is not UTF-8"
        ) from error


async def preflight_activation_ack_listener(
    *,
    binding: ActivationAcknowledgementBinding | None,
    certificate_path: str,
    read_trust_bundle: Callable[[dict[str, str]], Awaitable[str]],
    now: datetime | None = None,
    allow_test_trust: bool = False,
    minimum_remaining: timedelta = MIN_ACTIVATION_ACK_CERTIFICATE_REMAINING,
) -> dict[str, object]:
    """Validate the mounted leaf against the lock's pinned bundle, or refuse.

    `read_trust_bundle` is the cell adapter's reader, which fetches the
    immutable platform ConfigMap and checks its digest against the lock. Taking
    it as a callable keeps this testable without a Kubernetes client and keeps
    one reader for both the lifecycle path and startup.
    """

    if binding is None:
        # The environment says serve and the lock does not bind a protocol.
        # Serving anyway would put a listener in front of cells that have no
        # trust bundle for it.
        raise ActivationAckStartupRefusal(
            "activation acknowledgement listener is configured without a lock binding"
        )
    validated = binding.model_dump(mode="json")
    certificate_pem = _read_certificate(certificate_path)
    try:
        trust_pem = await read_trust_bundle(validated)
    except Exception as error:
        raise ActivationAckStartupRefusal(
            "activation acknowledgement trust bundle is unavailable"
        ) from error

    moment = datetime.now(UTC) if now is None else now
    try:
        report = validate_activation_ack_server_certificate(
            certificate_pem,
            platform_namespace=validated["platformNamespace"],
            trust_pem=trust_pem,
            now=moment,
            allow_test_trust=allow_test_trust,
            # Expiry is fatal; being inside the rotation window is not. The
            # caller decides what to do about the remaining time below.
            minimum_remaining=timedelta(0),
        )
    except ValueError as error:
        raise ActivationAckStartupRefusal(
            "activation acknowledgement server certificate is not the one the lock pins"
        ) from error

    remaining = int(report["remaining_seconds"])  # type: ignore[call-overload]
    if remaining < minimum_remaining.total_seconds():
        _LOG.warning(
            "",
            extra={
                "event": "activation-ack-certificate-rotation-due",
                "state": f"{remaining // 86_400}d-remaining",
            },
        )
    else:
        _LOG.info(
            "",
            extra={
                "event": "activation-ack-certificate-verified",
                "state": f"{remaining // 86_400}d-remaining",
            },
        )
    return report
