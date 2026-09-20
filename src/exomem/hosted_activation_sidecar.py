"""Compose the fixed hosted custody publisher and local acknowledgement service."""

from __future__ import annotations

import os
import signal
import stat
import threading
import time
from pathlib import Path

from .governance import authorization_custody as custody
from .governance import authorization_hosted_mount as mount
from .hosted_activation_delivery import CustodyDeliveryUnavailable

SOCKET_VOLUME_ROOT = Path("/run/exomem/activation-ack")
PLATFORM_NAMESPACE_ENV = "EXOMEM_HOSTED_ACTIVATION_ACK_PLATFORM_NAMESPACE"


def initialize_socket_directory(volume_root: Path = SOCKET_VOLUME_ROOT) -> None:
    """Create the owner-only subPath consumed by the sidecar and runtime.

    The init container alone mounts the emptyDir root. Serving containers mount
    only this subdirectory; no pod-wide fsGroup or PVC ownership change is used.
    """
    try:
        info = volume_root.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise CustodyDeliveryUnavailable
        descriptor = os.open(volume_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            try:
                os.mkdir("socket", mode=0o700, dir_fd=descriptor)
            except FileExistsError:
                pass
            private = os.stat("socket", dir_fd=descriptor, follow_symlinks=False)
            if (
                not stat.S_ISDIR(private.st_mode)
                or private.st_uid != os.geteuid()
                or private.st_gid != os.getegid()
                or stat.S_IMODE(private.st_mode) != 0o700
            ):
                raise CustodyDeliveryUnavailable
            if any((volume_root / "socket").iterdir()):
                raise CustodyDeliveryUnavailable
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except (OSError, RuntimeError, ValueError, TypeError):
        raise CustodyDeliveryUnavailable from None


def run_hosted_activation_sidecar(source: Path, destination: Path) -> int:
    from .hosted_activation_ack_client import is_hosted_activation_ack_enabled
    from .hosted_activation_ack_http import HostedActivationAckHttpClient
    from .hosted_activation_ack_server import (
        CustodyAcknowledgementService,
        HostedActivationAckUnixServer,
    )
    from .hosted_activation_publisher import CustodyPublisher

    stop = threading.Event()
    previous = {}
    server = None
    try:
        if not is_hosted_activation_ack_enabled():
            raise CustodyDeliveryUnavailable
        cell_id = os.environ.get("EXOMEM_HOSTED_CELL_ID", "")
        replica_id = os.environ.get(custody.REPLICA_ID_ENV, "")
        namespace = os.environ.get(PLATFORM_NAMESPACE_ENV, "")
        http = HostedActivationAckHttpClient.for_platform_namespace(namespace, cell_id=cell_id)
        publisher = CustodyPublisher(source, destination)
        service = CustodyAcknowledgementService(
            publisher, http, expected_cell_id=cell_id, expected_replica_id=replica_id
        )
        server = HostedActivationAckUnixServer(service)
        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGTERM, signal.SIGINT):
                previous[signum] = signal.signal(signum, lambda *_: stop.set())
        server.serve(
            stop,
            periodic_refresh=lambda: publisher.refresh(deadline=time.monotonic() + 5),
            refresh_interval_seconds=mount.WATCH_INTERVAL_SECONDS,
        )
        return 0
    except (OSError, RuntimeError, ValueError, TypeError):
        return 2
    finally:
        stop.set()
        if server is not None:
            server.stop()
        for signum, handler in previous.items():
            signal.signal(signum, handler)
