"""Tests for the Hetzner fake (task 3.5, 3.7, 3.8, 3.9)."""

from __future__ import annotations

from cellctl.storage.fake_hetzner import FakeHetznerVolumeProvider
from cellctl.storage.interface import VolumeInfo


def test_hetzner_fake_lists_and_finds_volumes() -> None:
    provider = FakeHetznerVolumeProvider(
        [VolumeInfo(volume_id="v1", server_id="s1", labels={"exomem.io/cloud-cell": "aaaaaaaaaaaaaaaa"})]
    )
    assert provider.get_volume("v1") is not None
    assert provider.get_volume("missing") is None
    provider.remove_volume("v1")
    assert provider.list_volumes() == []
