"""Tests for the B2/Hetzner fakes (task 3.5, 3.7, 3.8, 3.9)."""

from __future__ import annotations

import pytest

from cellctl.storage.fake_b2 import FakeB2
from cellctl.storage.fake_hetzner import FakeHetznerVolumeProvider
from cellctl.storage.interface import VolumeInfo


def test_create_prefix_key_restricts_to_the_cell_prefix() -> None:
    b2 = FakeB2()
    key = b2.create_prefix_key("aaaaaaaaaaaaaaaa")
    assert key.name_prefix == "cells/aaaaaaaaaaaaaaaa/"


def test_a_cells_key_cannot_list_another_cells_prefix() -> None:
    """Documents the real B2 property: a namePrefix-restricted application
    key (what a backup/restore Job authenticates as) is refused outside its
    own prefix. cellctl's own D10 calls are admin-scoped and do not go
    through this path; see test_a_cells_key_can_list_and_delete_its_own_prefix."""

    b2 = FakeB2()
    key_a = b2.create_prefix_key("aaaaaaaaaaaaaaaa")
    b2.seed_object("cells/bbbbbbbbbbbbbbbb/snapshot-1")
    with pytest.raises(PermissionError):
        b2.list_as_key(key_a.key_id, "cells/bbbbbbbbbbbbbbbb/")


def test_a_cells_key_cannot_delete_another_cells_object() -> None:
    b2 = FakeB2()
    key_a = b2.create_prefix_key("aaaaaaaaaaaaaaaa")
    version_id = b2.seed_object("cells/bbbbbbbbbbbbbbbb/snapshot-1")
    from cellctl.storage.interface import ObjectVersion

    with pytest.raises(PermissionError):
        b2.delete_as_key(
            key_a.key_id,
            ObjectVersion(key="cells/bbbbbbbbbbbbbbbb/snapshot-1", version_id=version_id),
        )


def test_a_cells_key_can_list_and_delete_its_own_prefix() -> None:
    b2 = FakeB2()
    key_a = b2.create_prefix_key("aaaaaaaaaaaaaaaa")
    b2.seed_object("cells/aaaaaaaaaaaaaaaa/snapshot-1")
    b2.seed_object("cells/aaaaaaaaaaaaaaaa/snapshot-1", version_id="hidden-version")

    versions = b2.list_as_key(key_a.key_id, "cells/aaaaaaaaaaaaaaaa/")
    assert len(versions) == 2

    for version in versions:
        b2.delete_as_key(key_a.key_id, version)
    assert b2.list_as_key(key_a.key_id, "cells/aaaaaaaaaaaaaaaa/") == []


def test_cellctl_itself_lists_and_deletes_with_admin_scope_no_key_needed() -> None:
    """cellctl's own D10 verification: works even after the per-cell key is
    gone, because it never authenticates as that key."""

    b2 = FakeB2()
    b2.seed_object("cells/aaaaaaaaaaaaaaaa/snapshot-1")
    assert len(b2.list_object_versions("cells/aaaaaaaaaaaaaaaa/")) == 1
    for version in b2.list_object_versions("cells/aaaaaaaaaaaaaaaa/"):
        b2.delete_object_version(version)
    assert b2.list_object_versions("cells/aaaaaaaaaaaaaaaa/") == []


def test_a_deleted_key_can_no_longer_be_used() -> None:
    b2 = FakeB2()
    key_a = b2.create_prefix_key("aaaaaaaaaaaaaaaa")
    b2.delete_key(key_a.key_id)
    assert not b2.key_exists(key_a.key_id)
    assert b2.key_absent(key_a.key_id)
    with pytest.raises(PermissionError):
        b2.list_as_key(key_a.key_id, "cells/aaaaaaaaaaaaaaaa/")


def test_hetzner_fake_lists_and_finds_volumes() -> None:
    provider = FakeHetznerVolumeProvider(
        [VolumeInfo(volume_id="v1", server_id="s1", labels={"exomem.io/cloud-cell": "aaaaaaaaaaaaaaaa"})]
    )
    assert provider.get_volume("v1") is not None
    assert provider.get_volume("missing") is None
    provider.remove_volume("v1")
    assert provider.list_volumes() == []
