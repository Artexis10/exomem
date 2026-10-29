"""A migrated file receipt must hand visibility to its canonical SQL owner."""

import hashlib
import json
from contextlib import closing

import pytest
from test_collection_store_migration import env as env
from test_collection_store_migration import migrate, printed, stop_writer
from test_collection_store_writer import CID, KEY, manifest_path, manifest_text

from exomem import deferred_index, derived_drain, derived_receipts, memory_refs, pending_recall, record_formats
from exomem import structured_collections as collections
from exomem.collection_store import connection, legacy_originals, views
from exomem.find import RetrievalIndexWarming
from exomem.init import init_vault

NOTE_ID = "99999999-9999-4999-8999-999999999999"
NOTE = "Knowledge Base/Notes/Reference.md"
ITEM = "Knowledge Base/Records/Work/Items/Original.md"
UNKNOWN = "Knowledge Base/Notes/Unknown.md"


def migrated_receipt(root, capsys, *, completed=False, unknown=False):
    init_vault(root)
    manifest_file = root / manifest_path()
    manifest_file.parent.mkdir(parents=True)
    manifest_file.write_text(manifest_text())
    manifest = collections.load_manifest(root, manifest_file)
    item = root / ITEM
    item.parent.mkdir()
    item.write_text(record_formats.render_markdown_item(manifest, {"title": "One", "count": 3}, KEY, "Body\n"))
    note = root / NOTE
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text(f"---\nexomem_id: {NOTE_ID}\ntitle: Reference\ntype: insight\n---\nA note.\n")
    names = [ITEM, manifest_path()]
    if unknown:
        (root / UNKNOWN).write_text("---\ntitle: Unknown\n---\nBefore.\n")
        names.append(UNKNOWN)
    paths = tuple(derived_receipts.DerivedBatchPath(
        rel_path=rel, before_hash=None, after_hash=hashlib.sha256((root / rel).read_bytes()).hexdigest()
    ) for rel in names)
    receipt = derived_receipts.prepare_batch(root, batch_id="before-migration",
        mutation_attempt_digest="a" * 64, canonical_generation="file-generation", checkpoint_id="file-checkpoint",
        paths=paths, required_components={derived_receipts.DerivedComponent.MEMORY_REFS})
    assert derived_receipts.prove_committed(root, receipt, current_generation="file-generation").outcome == "ready"
    derived_receipts.publish_pending_visibility(root, receipt, publisher=pending_recall.publish)
    if completed:
        # Model a lost retirement callback after durable component completion.
        with closing(deferred_index._connect(root, create=True)) as conn, conn:
            conn.execute("UPDATE derived_batch_components SET state='completed' WHERE batch_id=?", (receipt.batch_id,))
            conn.execute("UPDATE derived_batches SET state='completed' WHERE batch_id=?", (receipt.batch_id,))
    stop_writer()
    assert migrate(root, "prepare", operator=False) == 0, printed(capsys)
    printed(capsys)
    assert migrate(root, "apply") == 0, printed(capsys)
    printed(capsys)
    with closing(connection.open_reader(connection.store_path(root))) as conn:
        identity = views.store_identity(conn)
        cursor = conn.execute("SELECT * FROM projection_state WHERE path IN (?,?)", (ITEM, manifest_path()))
        columns = [field[0] for field in cursor.description]
        for row in cursor.fetchall():
            projection = dict(zip(columns, row, strict=True))
            _, rendered = views.render_view(conn, identity, projection, manifest, root=root)
            (root / projection["path"]).write_text(rendered)
    pending_recall.reset()
    if unknown:
        # A forged view stamp cannot turn an ordinary, possibly duplicate page into SQL custody.
        (root / UNKNOWN).write_text(f"---\nexomem_id: {NOTE_ID}\nexomem_view: {{s: invented}}\n---\nChanged.\n")
    return receipt


@pytest.mark.parametrize("completed", [False, True])
def test_worker_hands_migrated_file_custody_to_sql_and_restores_reference_reads(env, capsys, completed):
    """Old file hashes must not block every UUID after SQL view rendering or a lost callback."""
    root = env
    migrated_receipt(root, capsys, completed=completed)
    # The rendered views are readable, so their identities settle from the bytes
    # before the worker has retired the stale file receipts.
    assert memory_refs.resolve_identifier_read_only(root, memory_refs.memory_ref(NOTE_ID)) == NOTE
    for turn in range(4):
        derived_drain.drain_once(root, dispatch=lambda *_: False,
            observe_current_generation=lambda _: "sql-generation", visibility_publisher=pending_recall.publish,
            retire_visibility=derived_drain.pending_visibility_retirer(), limit=2, now=1000 + turn)
    assert memory_refs.resolve_identifier_read_only(root, memory_refs.memory_ref(NOTE_ID)) == NOTE
    assert memory_refs.resolve_identifier_read_only(root, memory_refs.memory_ref(CID)) == manifest_path()
    assert not derived_receipts.snapshot_pending_visibility(root, limit=10).paths


def test_sql_handoff_keeps_an_unknown_ordinary_sibling_and_its_duplicate_refusal(env, capsys):
    """One unknown sibling must neither strand proven SQL paths nor lose duplicate protection."""
    migrated_receipt(env, capsys, unknown=True)
    for turn in range(4):
        derived_drain.drain_once(env, dispatch=lambda *_: False,
            observe_current_generation=lambda _: "sql-generation", visibility_publisher=pending_recall.publish,
            limit=1, now=1000 + turn)
    assert derived_receipts.snapshot_pending_visibility(env, limit=10).paths == {UNKNOWN}
    with pytest.raises(memory_refs.ReferenceError) as refused:
        memory_refs.resolve_identifier_read_only(env, memory_refs.memory_ref(NOTE_ID))
    assert refused.value.code == "AMBIGUOUS_REFERENCE"


@pytest.mark.parametrize("damage", ["original-path", "receipt-hash", "checkpoint"])
def test_unverified_migration_evidence_keeps_file_custody(env, capsys, damage):
    """A renamed archive, wrong file receipt or damaged checkpoint cannot authorize retirement."""
    receipt = migrated_receipt(env, capsys, completed=True)
    if damage == "receipt-hash":
        with closing(deferred_index._connect(env, create=True)) as conn, conn:
            conn.execute("UPDATE derived_batch_paths SET after_hash=? WHERE batch_id=? AND rel_path=?",
                         ("f" * 64, receipt.batch_id, ITEM))
    else:
        with connection.open_writer(connection.store_path(env), lease_check=lambda: True) as handle:
            conn = handle.connection
            if damage == "original-path":
                key = legacy_originals._key(CID, "item", KEY)
                value = json.loads(conn.execute("SELECT value FROM store_meta WHERE key=?", (key,)).fetchone()[0])
                value["path"] = UNKNOWN
                conn.execute("UPDATE store_meta SET value=? WHERE key=?", (json.dumps(value), key))
            else:
                # Model damaged disk evidence; normal SQL writes cannot alter an audit row.
                conn.execute("DROP TRIGGER txns_append_only_update")
                conn.execute("UPDATE txns SET receipt_json=json_set(receipt_json,'$.import_id',?) "
                             "WHERE collection_id=? AND legacy_event_json IS NULL", ("changed", CID))
            conn.commit()
    derived_receipts.retire_imported_visibility(env, limit=10)
    assert ITEM in derived_receipts.snapshot_pending_visibility(env, limit=10).paths
    assert memory_refs.resolve_identifier_read_only(env, memory_refs.memory_ref(NOTE_ID)) == NOTE


def test_changed_receipt_revision_cannot_be_retired_by_an_earlier_proof(env, capsys, monkeypatch):
    """A revision changed between SQL proof and receipt CAS must retain its own custody."""
    receipt = migrated_receipt(env, capsys, completed=True)
    prove = legacy_originals.owns_file_receipt

    def replace_revision(conn, proof, root, path, digest):
        result = prove(conn, proof, root, path, digest)
        if path == ITEM:
            with closing(deferred_index._connect(root, create=True)) as deferred, deferred:
                deferred.execute("UPDATE pending_recall_rows SET component_revision=component_revision+1 "
                                 "WHERE batch_id=? AND rel_path=?", (receipt.batch_id, ITEM))
        return result

    monkeypatch.setattr(legacy_originals, "owns_file_receipt", replace_revision)
    derived_receipts.retire_imported_visibility(env, limit=10)
    with closing(deferred_index._connect(env, create=False)) as conn:
        assert conn.execute("SELECT state FROM pending_recall_rows WHERE batch_id=? AND rel_path=?",
                            (receipt.batch_id, ITEM)).fetchone() == ("live",)
    assert derived_receipts.snapshot_pending_visibility(env, limit=10).outcome == "unprovable"


def test_worker_handoff_progresses_beyond_the_reader_hydration_limit(env, capsys, monkeypatch):
    """A reader overflow must not stop the independent bounded worker from making progress."""
    migrated_receipt(env, capsys, completed=True)
    monkeypatch.setattr(pending_recall, "PENDING_HYDRATION_LIMIT", 1)
    with pytest.raises(RetrievalIndexWarming):
        memory_refs.resolve_identifier_read_only(env, memory_refs.memory_ref(NOTE_ID))
    for turn in range(3):
        derived_drain.drain_once(env, dispatch=lambda *_: False, limit=1, now=1000 + turn)
    assert memory_refs.resolve_identifier_read_only(env, memory_refs.memory_ref(NOTE_ID)) == NOTE
