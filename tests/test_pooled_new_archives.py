"""A new archive records that it starts in shared original storage; launcher support decides when."""

from __future__ import annotations

import datetime as dt
import logging
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


def _upload(vault: Path, filename: str = "receipt.png", data: bytes = PHOTO):
    return preserve.preserve_bytes(vault, scope="work", category="receipts", filename=filename,
                                   data=data, today=dt.date(2026, 11, 16))


def _objects(vault: Path) -> list[Path]:
    return sorted(original_pool.pool_root(vault).rglob("*.blob"))


@pytest.fixture
def unproven(monkeypatch):
    """No process has claimed launcher support yet, as for every vault creator in production."""
    monkeypatch.setattr(artifact_lifecycle, "_supported", frozenset())
    monkeypatch.setattr(original_pool, "_held", None)


def test_a_new_archive_reaches_shared_storage_at_its_first_capable_service_start(
        tmp_path, monkeypatch, unproven) -> None:
    """Catches the production gap: no creator proves launcher support, so a new archive that only its creator could
    activate stays ordinary for good. The service start after the standalone claim must activate it before writes."""
    from exomem import server

    vault = tmp_path / "vault"
    init.init_vault(vault)
    assert not original_pool.enabled(vault)
    monkeypatch.setattr(server, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    artifact_lifecycle.claim_standalone_support()  # What `server.run` claims for a server with no launcher.

    server.build_server(require_auth=False)
    filed = _upload(vault)

    assert _objects(vault) == [originals.pooled(vault, filed.path).local_path]
    assert originals.pooled(vault, filed.path).local_path.read_bytes() == PHOTO
    assert not (vault / filed.path).exists()
    assert not state_migration.shared_originals_intent(vault)
    assert original_pool.held_activation() is None


def test_a_capable_start_never_pools_an_existing_archive(vault, monkeypatch, unproven) -> None:
    """Catches a service start that moves an existing archive into shared storage by upgrading; only the owner's
    conversion may do that."""
    monkeypatch.setattr(artifact_lifecycle, "_supported", frozenset(artifact_lifecycle.LIFECYCLE_IDS))

    original_pool.activate_at_start(vault)

    assert not original_pool.enabled(vault)
    assert state_migration.SHARED_ORIGINALS_COMPATIBILITY_ID not in state_migration.recorded_descriptor_ids(vault)


def test_a_start_without_launcher_support_keeps_the_intent_and_names_the_update_until_a_capable_start(
        tmp_path, monkeypatch, caplog, unproven) -> None:
    """Catches a start that fails, drops the intent, or holds the layout without telling the operator which update
    unblocks it; and a later capable start that converts originals filed while the archive was ordinary."""
    from exomem import runtime_readiness

    vault = tmp_path / "vault"
    init.init_vault(vault)
    early = _upload(vault)
    monkeypatch.delenv("EXOMEM_VAULT_PATH", raising=False)

    with caplog.at_level(logging.WARNING, logger="exomem.original_pool"):
        original_pool.activate_at_start(vault)

    held = runtime_readiness.runtime_readiness(mcp_tool_surface_sha256="f" * 64)["original_storage"]
    assert held["reason"] == "launcher_support_unproven"
    assert "managed-service-upgrades" in held["action"]
    assert [record.levelno for record in caplog.records if "managed-service-upgrades" in record.getMessage()] == [
        logging.WARNING]
    assert state_migration.shared_originals_intent(vault)
    assert not original_pool.enabled(vault)

    monkeypatch.setattr(artifact_lifecycle, "_supported", frozenset(artifact_lifecycle.LIFECYCLE_IDS))
    original_pool.activate_at_start(vault)
    late = _upload(vault, "later.png", PHOTO + b"later")

    assert original_pool.held_activation() is None
    assert not state_migration.shared_originals_intent(vault)
    assert (vault / early.path).read_bytes() == PHOTO
    assert _objects(vault) == [originals.pooled(vault, late.path).local_path]


def test_a_new_cell_reaches_shared_storage_at_its_first_standalone_start(tmp_path, monkeypatch, unproven) -> None:
    """Catches a Cloud cell that stays ordinary: its cell-init proves no support, so its standalone start must."""
    vault = tmp_path / "volume" / "vault"
    cell_init.run_cell_init(vault)

    artifact_lifecycle.claim_standalone_support()
    original_pool.activate_at_start(vault)

    assert original_pool.enabled(vault)
    assert not state_migration.shared_originals_intent(vault)


def test_an_activation_interrupted_after_its_compatibility_record_completes_at_the_next_capable_start(
        tmp_path, monkeypatch, unproven) -> None:
    """Catches a half activation, format recorded and no marker, that refuses service or reads as already done."""
    vault = tmp_path / "vault"
    init.init_vault(vault)
    monkeypatch.setattr(artifact_lifecycle, "_supported", frozenset(artifact_lifecycle.LIFECYCLE_IDS))
    record = state_migration.record_shared_originals_compatibility

    def lost_after_record(*args, **kwargs):
        record(*args, **kwargs)
        raise RuntimeError("process lost before the marker")

    with monkeypatch.context() as interrupted:
        interrupted.setattr(state_migration, "record_shared_originals_compatibility", lost_after_record)
        original_pool.activate_at_start(vault)
    assert original_pool.held_activation()["reason"] == "activation_failed"
    assert state_migration.SHARED_ORIGINALS_COMPATIBILITY_ID in state_migration.recorded_descriptor_ids(vault)
    assert not original_pool.enabled(vault)
    state_migration.require_vault_state_ready(vault)

    original_pool.activate_at_start(vault)
    filed = _upload(vault)

    assert original_pool.enabled(vault)
    assert not state_migration.shared_originals_intent(vault)
    assert _objects(vault) == [originals.pooled(vault, filed.path).local_path]


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
    assert not state_migration.shared_originals_intent(vault)


def test_a_new_archive_without_launcher_support_records_its_intent_and_says_when_it_starts(
        tmp_path, monkeypatch) -> None:
    """Catches an init that fails, enrols a format an older launcher cannot serve, or forgets that the archive is new,
    where no support is proven."""
    monkeypatch.setattr(artifact_lifecycle, "_supported", frozenset())
    vault = tmp_path / "vault"

    report = init.init_vault(vault)
    filed = _upload(vault)

    assert report["original_storage"]["layout"] == "ordinary"
    assert "first service start" in report["original_storage"]["action"]
    assert "managed-service-upgrades" in report["original_storage"]["action"]
    assert not original_pool.pool_root(vault).exists()
    assert state_migration.SHARED_ORIGINALS_COMPATIBILITY_ID not in state_migration.recorded_descriptor_ids(vault)
    assert state_migration.shared_originals_intent(vault)
    assert (vault / filed.path).read_bytes() == PHOTO


def test_an_overlay_of_an_existing_archive_never_changes_its_original_storage(vault, monkeypatch) -> None:
    """Catches a scaffold overlay that switches an existing archive's layout, or marks it new, without the owner's
    conversion."""
    monkeypatch.setattr(artifact_lifecycle, "_supported", frozenset(artifact_lifecycle.LIFECYCLE_IDS))

    report = init.init_vault(vault, force=True)

    assert "original_storage" not in report
    assert not original_pool.enabled(vault)
    assert not state_migration.shared_originals_intent(vault)


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
    assert state_migration.shared_originals_intent(vault) is not supported
    assert result.original_storage["layout"] == ("shared" if supported else "ordinary")
    assert cell_init.run_cell_init(vault).original_storage is None  # An existing cell archive is never re-laid out.
