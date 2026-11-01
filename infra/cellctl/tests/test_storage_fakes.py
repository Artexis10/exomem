"""Tests for the B2/Hetzner fakes (task 3.5, 3.7, 3.8, 3.9)."""

from __future__ import annotations

from cellctl.storage.fake_b2 import FakeB2
from cellctl.storage.fake_hetzner import FakeHetznerVolumeProvider
from cellctl.storage.interface import VolumeInfo


def test_cellctl_lists_and_deletes_object_versions_without_any_key() -> None:
    b2 = FakeB2()
    b2.seed_object("cells/aaaaaaaaaaaaaaaa/snapshot-1")
    b2.seed_object("cells/aaaaaaaaaaaaaaaa/snapshot-1", version_id="hidden-version")
    versions = b2.list_object_versions("cells/aaaaaaaaaaaaaaaa/")
    assert len(versions) == 2
    for version in versions:
        b2.delete_object_version(version)
    assert b2.list_object_versions("cells/aaaaaaaaaaaaaaaa/") == []


def test_hetzner_fake_lists_and_finds_volumes() -> None:
    provider = FakeHetznerVolumeProvider(
        [VolumeInfo(volume_id="v1", server_id="s1", labels={"exomem.io/cloud-cell": "aaaaaaaaaaaaaaaa"})]
    )
    assert provider.get_volume("v1") is not None
    assert provider.get_volume("missing") is None
    provider.remove_volume("v1")
    assert provider.list_volumes() == []
