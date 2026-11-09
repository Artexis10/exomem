"""A writer that bypasses ``batch_atomic_write`` commits to the store in a store-mode vault.

OpenSpec make-vault-store-canonical task 2.2. The vault is the migration suite's generated vault in
store mode, and the writer is bound as the store thread binds it (``require_servable`` still refuses
a store-mode service start, so no service holds the store here). The projector is off unless a test
runs its pass by hand: nothing writes a stored page to disk, so each answer comes from the store.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
from contextlib import closing

import pytest
import test_vault_store_read_write as read_write
import test_vault_store_write_path as write_path
from test_managed_collections_migration import coordinator_url  # noqa: F401
from test_reconcile import _LIFECYCLE_IDS, _install_lifecycle_slot
from test_vault_store_migration import ALPHA, BETA, DIAGRAM, vault  # noqa: F401
from test_vault_store_write_path import (
    bm25_paths,
    edge_targets,
    planned,
    revision,
    store_rows,
    tree,
)

from exomem import (
    delete_directory,
    delete_file,
    move_file,
    recover_from_trash,
    registry_history,
    relation_review,
    reserved_paths,
    scene_frames,
    semantic_writes,
)
from exomem import vault as vault_module
from exomem.cli_ops import OpError
from exomem.collection_store import connection
from exomem.governance import membership, policy
from exomem.governance.principal import owner_principal
from exomem.governance.tool import op_govern_memory
from exomem.vault_store import availability, batch, projection, read, verify, write

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="The offline-owner lease is Linux-only"
)

# The fixtures of the suites that build this vault, re-exported by assignment: a test that takes one
# is then no redefinition of an import.
stored, writer, served = read_write.stored, read_write.writer, write_path.served

GAMMA = "Knowledge Base/Notes/Gamma.md"
GAMMA_TEXT = (
    "---\ntitle: Gamma\ntype: note\n---\n# Gamma\n\nThe harbour master keeps the tide ledger.\n"
)


def store_gamma(root):
    """A page that only the store holds: it has no file until the projector writes one."""
    vault_module.batch_atomic_write(
        [planned(root, GAMMA, GAMMA_TEXT, create_only=True)],
        vault_root=root,
        post_commit_fanout=False,
    )


def stored_bytes(root, rel):
    return read.owned_bytes(root, rel)


def new_files(before, root):
    """The files that appeared on disk since ``before``, and any file whose bytes changed."""
    after = tree(root)
    return {path.relative_to(root).as_posix() for path in after if before.get(path) != after[path]}


def test_delete_moves_a_stored_page_to_the_trash_with_its_rows_and_writes_no_markdown(served):
    """A page deleted in store mode that leaves rows, or Markdown bytes, behind at its old path."""
    store_gamma(served)
    assert bm25_paths(served, "harbour") == [GAMMA]
    before = tree(served)

    result = delete_file.delete_file(served, path=GAMMA, confirm=True)

    assert not store_rows(served, "SELECT 1 FROM store_pages WHERE path = ?", GAMMA)
    assert stored_bytes(served, result.trash_path) == GAMMA_TEXT.encode()
    assert bm25_paths(served, "harbour") == []
    assert edge_targets(served, GAMMA) == set()
    # The only file that appears is the metadata sidecar, which is a binary and goes to disk first.
    assert new_files(before, served) == {result.trash_meta_path}
    assert result.index["components"], "the report names the row work that the commit did"
    assert not (served / result.trash_path).exists()


def test_delete_of_a_page_that_a_competing_commit_changed_refuses_and_leaves_no_sidecar(
    served, writer, monkeypatch
):
    """A delete that wins a race against an edit and trashes bytes that it never read."""
    store_gamma(served)
    original_check = batch._check_held
    raced = []

    def check_then_race(plan):
        original_check(plan)
        if not raced:
            raced.append(1)
            write.commit(
                writer,
                write.StoreChange(
                    write.Origin.GOVERNED, "race", puts=(write.PagePut(GAMMA, b"# Gamma, raced\n"),)
                ),
            )

    monkeypatch.setattr(batch, "_check_held", check_then_race)
    before = tree(served)
    start = revision(served)

    with pytest.raises(delete_file.DeleteFileError) as refused:
        delete_file.delete_file(served, path=GAMMA, confirm=True)

    assert refused.value.code == "STALE_DELETE"
    assert revision(served) == start + 1, "only the competing commit may have committed"
    assert stored_bytes(served, GAMMA) == b"# Gamma, raced\n"
    assert tree(served) == before, "the refused commit must roll back the sidecar it published"


def gamma_rows(root):
    """Every derived row of the page, by the column that names it in each table."""
    return {
        "pages": store_rows(root, "SELECT * FROM pages WHERE path = ?", GAMMA),
        "edges": store_rows(
            root, "SELECT * FROM graph_edges WHERE source_path = ? ORDER BY 1", GAMMA
        ),
        "identities": store_rows(root, "SELECT * FROM identities WHERE path = ?", GAMMA),
        "search": bm25_paths(root, "harbour"),
    }


def test_a_delete_then_a_move_back_through_the_store_restores_the_page_and_rows(served, writer):
    """A trash that loses the page, or leaves rows that differ from the ones it had before."""
    store_gamma(served)
    rows_before = gamma_rows(served)
    assert rows_before["pages"], "the page must have rows for this check to mean anything"
    result = delete_file.delete_file(served, path=GAMMA, confirm=True)
    assert gamma_rows(served)["pages"] == []

    trashed = hashlib.sha256(stored_bytes(served, result.trash_path)).hexdigest()
    # The recovery writer refuses in store mode; its store form is this one transaction.
    batch.commit_batch(
        writer,
        [],
        vault_root=served,
        lifecycle=batch.Lifecycle(
            moves=(write.PageMove(result.trash_path, GAMMA),),
            binary_deletes=(write.BinaryDelete(result.trash_meta_path),),
            expect={result.trash_path: write.Expect(sha256=trashed)},
        ),
    )

    assert stored_bytes(served, GAMMA) == GAMMA_TEXT.encode()
    assert not store_rows(served, "SELECT 1 FROM store_pages WHERE path = ?", result.trash_path)
    assert gamma_rows(served) == rows_before


def test_the_rows_of_a_delete_equal_a_cold_derivation_from_the_files(served, tmp_path):
    """Rows a delete commits that differ from what file mode derives from the same vault."""
    store_gamma(served)
    delete_file.delete_file(served, path=GAMMA, confirm=True)
    # The projector's step, run by hand as the write-path suite runs it: each stored page becomes
    # its file, with its stored mtime.
    with read.snapshot(served) as view:
        for meta in view.pages():
            target = projection.write_file(
                served, meta.path, view.page(meta.path).body, durable=False
            )
            os.utime(target, ns=(meta.mtime_ns, meta.mtime_ns))

    expected = tmp_path / "expected.sqlite"
    verify.derive_expected(served, expected)
    with closing(sqlite3.connect(connection.store_path(served))) as store:
        comparison = verify.compare_tables(store, expected)
    assert comparison.tables, "the comparison must cover tables for this check to mean anything"
    # A trigger stamps a revision key for a parent whose page rows go, in file mode as here; a cold
    # build creates none, so that one key-only table is the only place the two may differ.
    assert [d for d in comparison.differences if d.table != "unit_revisions"] == []


def govern(root, rel):
    """A real policy file pair that covers ``rel``, as the lifecycle suites write them."""
    governance = root / "Knowledge Base/_Governance"
    (governance / "scopes").mkdir(parents=True)
    (governance / "rules").mkdir(parents=True)
    (governance / "scopes/lifecycle.yaml").write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\n"
        f'paths: ["{rel.removeprefix("Knowledge Base/")}"]\n',
        encoding="utf-8",
    )
    (governance / "rules/lifecycle.yaml").write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FB0\n"
        'scope_ids: ["01ARZ3NDEKTSV4RRFFQ69G5FAV"]\naudience: external\nceiling: 4\n',
        encoding="utf-8",
    )
    policy._CACHE.clear()
    membership.clear_memo()


def test_delete_of_a_governed_page_refuses_by_name_and_changes_nothing(served):
    """A delete that skips the tombstone and receipt that a governed page's trash writes."""
    govern(served, BETA)
    before = tree(served)
    start = revision(served)

    with pytest.raises(OpError) as refused:
        delete_file.delete_file(served, path=BETA, confirm=True, force_orphan=True)

    assert refused.value.code == availability.WRITER_UNROUTED
    assert "delete_file" in refused.value.message
    assert revision(served) == start
    assert tree(served) == before
    assert read.owned_bytes(served, BETA)


def test_delete_of_a_binary_refuses_by_name_and_changes_nothing(served):
    """A binary that the store records, with its bytes on disk: the delete must not unlink them."""
    before = tree(served)
    start = revision(served)

    with pytest.raises(OpError) as refused:
        delete_file.delete_file(served, path=DIAGRAM, confirm=True)

    assert refused.value.code == availability.WRITER_UNROUTED
    assert revision(served) == start
    assert tree(served) == before


def test_move_recover_and_folder_delete_refuse_by_name_and_change_nothing(served):
    """Writers whose semantic check reads the disk: each must refuse before it renames anything."""
    before = tree(served)
    start = revision(served)

    refusals = {
        "move_file": lambda: move_file.move_file(
            served, old_path=BETA, new_path="Knowledge Base/Notes/Beta Two.md"
        ),
        "recover_from_trash": lambda: recover_from_trash.recover_from_trash(
            served, trash_path="Knowledge Base/_trash/2026-01-01/000000-Notes__Beta.md"
        ),
        "delete_directory": lambda: delete_directory.delete_directory(
            served, path="Knowledge Base/Notes", confirm=True
        ),
    }
    for operation, attempt in refusals.items():
        with pytest.raises(OpError) as refused:
            attempt()
        assert refused.value.code == availability.WRITER_UNROUTED, operation
        assert operation in refused.value.message

    assert revision(served) == start
    assert tree(served) == before


def history_name(index):
    """A snapshot name in the form that ``registry_history`` writes: a UTC stamp and a hash tag."""
    return f"20260101T{index:012d}Z-abcdef01"


def test_a_registry_history_prune_deletes_from_the_store_snapshots_that_have_no_file(served):
    """A prune that lists the disk finds no stored snapshot and keeps them all."""
    stem = "roles"
    folder = f"Knowledge Base/_Schema/history/{stem}"
    count = registry_history.HISTORY_KEEP + 3
    names = [history_name(index) for index in range(count)]
    vault_module.batch_atomic_write(
        [
            planned(served, f"{folder}/{name}.yaml", "schema_version: 1\n", create_only=True)
            for name in names
        ],
        vault_root=served,
        post_commit_fanout=False,
    )
    assert not (served / folder).exists(), "no file may exist for this check to mean anything"
    before = tree(served)

    registry_history._prune(served, stem)

    with read.snapshot(served) as view:
        kept = [meta.path for meta in view.pages(f"{folder}/")]
    assert kept == [f"{folder}/{name}.yaml" for name in names[3:]], "the newest are kept"
    assert not store_rows(served, "SELECT 1 FROM pages WHERE path = ?", f"{folder}/{names[0]}.yaml")
    assert tree(served) == before, "no Markdown or YAML file may be written or removed"


def test_lifecycle_prepared_cleanup_refuses_by_name_and_changes_nothing(served):
    """A stale judgement made from the files that deletes a slot whose page the store holds."""
    page_id = _LIFECYCLE_IDS["stale"]
    _, prepared, _, _ = _install_lifecycle_slot(
        served, state="stale", page_id=page_id, path="Knowledge Base/Notes/Insights/late-owner.md"
    )
    posthoc = semantic_writes.evaluate_posthoc_batch(served, operation="reconcile")
    inspected = relation_review.inspect_lifecycle_prepared_slots(served, corpus=posthoc.corpus)
    assert [item.cleanup_eligible for item in inspected.inspections] == [True]
    before = tree(served)
    start = revision(served)

    with pytest.raises(OpError) as refused:
        relation_review.cleanup_stale_lifecycle_prepared_batch(served, inspected.inspections)

    assert refused.value.code == availability.WRITER_UNROUTED
    assert "lifecycle prepared cleanup" in refused.value.message
    assert prepared.exists()
    assert revision(served) == start
    assert tree(served) == before


def test_companion_backfill_commit_refuses_by_name_before_it_arms_a_journal(served):
    """A backfill that publishes its companion page to disk where the store holds the page."""
    before = tree(served)
    start = revision(served)

    with reserved_paths._owner_authority_scope("govern_memory"), pytest.raises(OpError) as refused:
        op_govern_memory(
            served,
            operation="backfill_companion",
            backfill_action="commit",
            proposal_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
            companion_input={},
            principal=owner_principal(),
        )

    assert refused.value.code == availability.WRITER_UNROUTED
    assert "companion_backfill" in refused.value.message
    assert revision(served) == start
    assert tree(served) == before


def test_scene_frame_clear_refuses_a_stored_sidecar_and_passes_a_video_without_frames(served):
    """A clear that unlinks projections and leaves the stored sidecar pages behind."""
    video = served / "Knowledge Base/Media/harbour.mp4"
    sidecar = "Knowledge Base/Media/harbour.mp4.frames/scene-000-t0ms.jpg.md"
    assert scene_frames.clear_scene_frames(served, video) == 0, "no frames: it clears as before"
    vault_module.batch_atomic_write(
        [planned(served, sidecar, "# Frame\n", create_only=True)],
        vault_root=served,
        post_commit_fanout=False,
    )
    before = tree(served)
    start = revision(served)

    with pytest.raises(OpError) as refused:
        scene_frames.clear_scene_frames(served, video)

    assert refused.value.code == availability.WRITER_UNROUTED
    assert revision(served) == start
    assert tree(served) == before
    assert read.owned_bytes(served, sidecar) == b"# Frame\n"


def test_the_generic_path_writers_refuse_a_stored_page_and_pass_a_binary(served):
    """A publish, unlink or move of a stored page that skips the store; a sidecar still passes."""
    before = tree(served)
    start = revision(served)

    attempts = {
        "publish_generic_bytes": lambda: reserved_paths.publish_generic_bytes(
            served, BETA, b"# Overwritten\n", expected_identity=None
        ),
        "unlink_generic_file": lambda: reserved_paths.unlink_generic_file(served, BETA),
        "move_generic_path": lambda: reserved_paths.move_generic_path(
            served, BETA, "Knowledge Base/Notes/Beta Two.md", source_kind="file"
        ),
    }
    for operation, attempt in attempts.items():
        with pytest.raises(OpError) as refused:
            attempt()
        assert refused.value.code == availability.WRITER_UNROUTED, operation
        assert operation in refused.value.message

    assert revision(served) == start
    assert tree(served) == before
    # A metadata sidecar is a binary: its bytes go to disk first, whatever the mode.
    reserved_paths.publish_generic_bytes(
        served, f"{DIAGRAM}.meta.json", b"{}", expected_identity=None
    )
    assert (served / f"{DIAGRAM}.meta.json").read_bytes() == b"{}"
