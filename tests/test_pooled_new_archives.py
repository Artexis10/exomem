"""A new archive starts in shared original storage only where launcher support is proven."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from exomem import (
    artifact_lifecycle,
    cell_init,
    init,
    original_pool,
    originals,
    preserve,
    state_migration,
)

PHOTO = b"\x89PNG\r\n\x1a\n" + b"receipt photo bytes" * 64


def _upload(vault: Path):
    return preserve.preserve_bytes(vault, scope="work", category="receipts", filename="receipt.png",
                                   data=PHOTO, today=dt.date(2026, 11, 16))


def test_a_new_archive_starts_in_shared_storage_and_files_an_upload_as_one_object(tmp_path, monkeypatch) -> None:
    """Catches a new archive that keeps the ordinary layout, which only an owner conversion could undo later."""
    monkeypatch.setattr(artifact_lifecycle, "_supported", frozenset(artifact_lifecycle.LIFECYCLE_IDS))
    vault = tmp_path / "vault"

    report = init.init_vault(vault)
    filed = _upload(vault)

    assert not (vault / filed.path).exists()
    assert originals.pooled(vault, filed.path).local_path.read_bytes() == PHOTO
    assert state_migration.SHARED_ORIGINALS_COMPATIBILITY_ID in state_migration.recorded_descriptor_ids(vault)
    assert report["original_storage"] == {"layout": "shared"}


def test_a_new_archive_without_launcher_support_keeps_ordinary_storage_and_says_why(tmp_path, monkeypatch) -> None:
    """Catches an init that fails, or enrols a format an older launcher cannot serve, where no support is proven."""
    monkeypatch.setattr(artifact_lifecycle, "_supported", frozenset())
    vault = tmp_path / "vault"

    report = init.init_vault(vault)
    filed = _upload(vault)

    assert report["original_storage"]["layout"] == "ordinary"
    assert "convert-originals" in report["original_storage"]["action"]
    assert "managed-service-upgrades" in report["original_storage"]["action"]
    assert not original_pool.pool_root(vault).exists()
    assert state_migration.SHARED_ORIGINALS_COMPATIBILITY_ID not in state_migration.recorded_descriptor_ids(vault)
    assert (vault / filed.path).read_bytes() == PHOTO


def test_an_overlay_of_an_existing_archive_never_changes_its_original_storage(tmp_path, monkeypatch) -> None:
    """Catches a scaffold overlay that switches an existing archive's layout without the owner's conversion."""
    vault = tmp_path / "vault"
    monkeypatch.setattr(artifact_lifecycle, "_supported", frozenset())
    init.init_vault(vault)
    monkeypatch.setattr(artifact_lifecycle, "_supported", frozenset(artifact_lifecycle.LIFECYCLE_IDS))

    report = init.init_vault(vault, force=True)

    assert "original_storage" not in report
    assert not original_pool.enabled(vault)


@pytest.mark.parametrize("supported", [True, False])
def test_a_new_cell_archive_follows_the_new_archive_rule_at_its_final_path(tmp_path, monkeypatch, supported) -> None:
    """Catches a cell archive left ordinary, or a format requirement recorded for the staging path it renames away."""
    monkeypatch.setattr(artifact_lifecycle, "_supported",
                        frozenset(artifact_lifecycle.LIFECYCLE_IDS) if supported else frozenset())
    vault = tmp_path / "volume" / "vault"

    result = cell_init.run_cell_init(vault)

    assert original_pool.enabled(vault) is supported
    recorded = state_migration.SHARED_ORIGINALS_COMPATIBILITY_ID in state_migration.recorded_descriptor_ids(vault)
    assert recorded is supported
    assert result.original_storage["layout"] == ("shared" if supported else "ordinary")
    assert cell_init.run_cell_init(vault).original_storage is None  # An existing cell archive is never re-laid out.
