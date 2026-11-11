"""A governed page's trash and recovery keep their tombstone and receipt around the store commit.

OpenSpec make-vault-store-canonical task 2.5 (design D7). The vault is the migration suite's
generated vault, moved into store mode by the real migrate route, with the writer bound as the store
thread binds it. ``govern`` writes a policy that restricts ``BETA``. The projector is off, so a page
that a commit moves keeps its old file on disk: a step that decided from the disk would see the page
where it was.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import test_vault_store_read_write as read_write
import test_vault_store_write_path as write_path
from test_managed_collections_migration import coordinator_url  # noqa: F401
from test_vault_store_bypass_writers import GAMMA, govern, store_gamma
from test_vault_store_migration import BETA, vault  # noqa: F401
from test_vault_store_write_path import revision, store_rows

from exomem import delete_directory, delete_file, embeddings, recover_from_trash
from exomem.governance import lifecycle, receipts

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="The offline-owner lease is Linux-only"
)

stored, writer, served = read_write.stored, read_write.writer, write_path.served

TOMBSTONES = "Knowledge Base/_Governance/deletion-tombstones"


def tombstones(root: Path) -> list[dict]:
    """The deletion tombstones and recovery guards under the governance folder, as written."""
    folder = root / TOMBSTONES
    return [json.loads(path.read_text()) for path in sorted(folder.glob("*.json"))]


def phases(root: Path) -> list[tuple[str, str]]:
    """Each receipt event's type and phase, in the order the event log holds them."""
    return [(row["event_type"], row["phase"]) for row in receipts.event_records(root)]


def stored_page(root: Path, rel: str) -> bool:
    return bool(store_rows(root, "SELECT 1 FROM store_pages WHERE path = ?", rel))


def seed_vectors(root: Path, rel: str) -> None:
    """One vector row for ``rel`` in the vector sidecar, as the drain would have left it."""
    connection = embeddings.get_embedding_index(root)._connect()
    try:
        with connection:
            connection.execute(
                "INSERT INTO chunks (file_path, chunk_idx, chunk_text, vector, file_mtime) "
                "VALUES (?, 0, 'tide', ?, 0)",
                (rel, b"\0\0\0\0"),
            )
    finally:
        connection.close()


def vector_rows(root: Path, rel: str) -> int:
    connection = embeddings.get_embedding_index(root)._connect()
    try:
        return connection.execute(
            "SELECT COUNT(*) FROM chunks WHERE file_path = ?", (rel,)
        ).fetchone()[0]
    finally:
        connection.close()


def test_a_governed_delete_settles_its_tombstone_and_receipt_from_the_store(served, monkeypatch):
    """A governed trash in store mode whose receipt never closes, or whose content a restricted
    caller could read between the commit and the settling."""
    govern(served, BETA)
    seed_vectors(served, BETA)
    seen: dict[str, object] = {}

    def observe(point: str) -> None:
        if point == "deletion_moved":
            seen["pending"] = [marker["state"] for marker in tombstones(served)]
            seen["withheld"] = lifecycle.is_tombstoned(served, BETA)

    monkeypatch.setattr(lifecycle, "_checkpoint", observe)

    result = delete_file.delete_file(served, path=BETA, confirm=True, force_orphan=True)

    assert seen == {"pending": ["pending"], "withheld": True}
    assert not stored_page(served, BETA) and stored_page(served, result.trash_path)
    assert vector_rows(served, BETA) == 0
    assert [marker["state"] for marker in tombstones(served)] == ["committed"]
    assert [phase for _type, phase in phases(served) if phase in {"committed", "aborted"}] == [
        "committed"
    ]
    assert [kind for kind, _phase in phases(served)].count("deletion") == 1
    assert lifecycle.is_tombstoned(served, BETA)
    assert lifecycle.is_tombstoned(served, result.trash_path)
    assert not any("not exact" in warning for warning in result.warnings)
    assert "embeddings" in {item["component"] for item in result.index["components"]}


@pytest.mark.parametrize(
    ("crash_point", "terminal"),
    [
        ("deletion_intent", "aborted"),
        ("deletion_tombstone", "aborted"),
        ("deletion_moved", "committed"),
        ("deletion_record", "committed"),
        ("deletion_terminal", "committed"),
    ],
)
def test_a_governed_delete_killed_at_each_crash_point_reconciles_once_from_the_store(
    served, monkeypatch, crash_point, terminal
):
    """A kill around the store commit that reconcile settles the wrong way: a page that the commit
    trashed whose old file is still on disk read as never trashed, or a receipt settled twice."""
    govern(served, BETA)
    start = revision(served)

    def crash(point: str) -> None:
        if point == crash_point:
            raise RuntimeError("simulated lifecycle crash")

    monkeypatch.setattr(lifecycle, "_checkpoint", crash)
    with pytest.raises(RuntimeError, match="simulated lifecycle crash"):
        delete_file.delete_file(served, path=BETA, confirm=True, force_orphan=True)
    monkeypatch.setattr(lifecycle, "_checkpoint", lambda _point: None)

    first = receipts.reconcile(served, dry_run=False)
    second = receipts.reconcile(served, dry_run=False)

    terminals = [phase for _type, phase in phases(served) if phase in {"committed", "aborted"}]
    assert terminals == [terminal]
    assert first["repairs"] and second["repairs"] == []
    trashed = revision(served) == start + 1
    assert trashed is (terminal == "committed")
    assert stored_page(served, BETA) is not trashed
    # The projector is off: BETA's old file stays on disk either way.
    assert (served / BETA).exists()
    if terminal == "aborted":
        assert tombstones(served) == []
    else:
        assert [marker["state"] for marker in tombstones(served)] == ["committed"]


def trashed_beta(root: Path) -> str:
    """Trash the governed page through the store and return where it went."""
    govern(root, BETA)
    return delete_file.delete_file(root, path=BETA, confirm=True, force_orphan=True).trash_path


def test_a_governed_recovery_finds_the_tombstone_lineage_and_clears_it(served):
    """A recovery in store mode that judges its restored rows from the file-mode sidecar files, so
    that the page comes back and stays tombstoned from every restricted caller."""
    trash_path = trashed_beta(served)

    result = recover_from_trash.recover_from_trash(served, trash_path=trash_path)

    assert stored_page(served, BETA) and not stored_page(served, trash_path)
    assert tombstones(served) == []
    assert not lifecycle.is_tombstoned(served, BETA)
    assert not any("not exact" in warning for warning in result.warnings)
    recovered = [row for row in receipts.event_records(served) if row["event_type"] == "recovery"]
    assert len(recovered) == 1


@pytest.mark.parametrize(
    ("crash_point", "terminal"),
    [
        ("recovery_intent", "aborted"),
        ("recovery_marker", "aborted"),
        ("recovery_moved", "committed"),
        ("recovery_record", "committed"),
        ("recovery_terminal", "committed"),
    ],
)
def test_a_governed_recovery_killed_at_each_crash_point_reconciles_once_from_the_store(
    served, monkeypatch, crash_point, terminal
):
    """A kill around a recovery's store commit that reconcile settles from the projection, which
    still shows the page in the trash, or settles twice."""
    trash_path = trashed_beta(served)
    start = revision(served)

    def crash(point: str) -> None:
        if point == crash_point:
            raise RuntimeError("simulated recovery crash")

    monkeypatch.setattr(lifecycle, "_checkpoint", crash)
    with pytest.raises(RuntimeError, match="simulated recovery crash"):
        recover_from_trash.recover_from_trash(served, trash_path=trash_path)
    monkeypatch.setattr(lifecycle, "_checkpoint", lambda _point: None)

    first = receipts.reconcile(served, dry_run=False)
    second = receipts.reconcile(served, dry_run=False)

    recovery_ids = {
        row["event_id"]
        for row in receipts.event_records(served)
        if row.get("operation") == "governed_recovery"
    }
    settled = [
        row["phase"]
        for row in receipts.event_records(served)
        if row.get("causation_id") in recovery_ids and row["phase"] in {"committed", "aborted"}
    ]
    assert settled == [terminal]
    assert first["repairs"] and second["repairs"] == []
    restored = revision(served) == start + 1
    assert restored is (terminal == "committed")
    assert stored_page(served, BETA) is restored
    assert lifecycle.is_tombstoned(served, BETA) is not restored


def test_an_ungoverned_page_on_a_governed_vault_trashes_with_no_tombstone_or_receipt(served):
    """The vault-wide refusal coming back, or a tombstone written for a page no policy covers."""
    govern(served, BETA)
    store_gamma(served)

    result = delete_file.delete_file(served, path=GAMMA, confirm=True)

    assert stored_page(served, result.trash_path)
    assert tombstones(served) == []
    assert receipts.event_records(served) == []


def test_a_governed_folder_trash_names_every_item_in_one_tombstone(served):
    """A folder trash in store mode that settles a governed page's receipt without the folder's
    other items, or that reads the folder from its projection."""
    govern(served, BETA)
    folder = str(Path(BETA).parent)

    result = delete_directory.delete_directory(
        served, path=folder, confirm=True, recursive=True, force_orphan=True
    )

    (marker,) = tombstones(served)
    assert marker["state"] == "committed"
    trashed = {item["source_path"] for item in marker["manifest"]}
    assert BETA in trashed and len(trashed) == result.file_count
    assert not stored_page(served, BETA)
