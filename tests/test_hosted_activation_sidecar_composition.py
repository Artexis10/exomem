from __future__ import annotations

import os

import pytest

from exomem.hosted_activation_delivery import CustodyDeliveryUnavailable
from exomem.hosted_activation_sidecar import initialize_socket_directory

pytestmark = pytest.mark.skipif(os.name == "nt", reason="Hosted sidecar is Linux-only")


def test_initializer_creates_only_a_private_owned_socket_subdirectory(tmp_path):
    mount = tmp_path / "socket-volume"
    mount.mkdir(mode=0o777)
    initialize_socket_directory(mount)
    private = mount / "socket"
    assert private.is_dir()
    assert private.stat().st_mode & 0o777 == 0o700
    assert private.stat().st_uid == os.geteuid()
    assert list(private.iterdir()) == []
    identity = private.stat().st_ino
    initialize_socket_directory(mount)
    assert private.stat().st_ino == identity
    (private / "in-use").touch()
    with pytest.raises(CustodyDeliveryUnavailable):
        initialize_socket_directory(mount)


def test_initializer_refuses_symlinked_volume(tmp_path):
    actual = tmp_path / "actual"
    actual.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(actual, target_is_directory=True)
    with pytest.raises(CustodyDeliveryUnavailable):
        initialize_socket_directory(alias)
    assert list(actual.iterdir()) == []
