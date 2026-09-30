"""Dark built-in writer contracts, including durable transport retry recovery."""

import json
import multiprocessing
from contextlib import closing
from pathlib import Path

import pytest

from exomem import mutation_terminal, structured_collections as collections
from exomem.collection_store import chain, connection, tokens

CID = "2db90f18-70df-4e41-986e-2d7d7db1caca"
KEY = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"


def manifest_text(profile="records", layout="markdown-items"):
    fields = "    title: {type: string, required: true}\n    count: {type: integer}\n"
    if profile == "planning":
        fields = "".join(
            f"    {name}: {{type: string{', required: true' if name == 'title' else ''}}}\n"
            for name in (
                "title",
                "kind",
                "status",
                "lifecycle",
                "priority",
                "commitment",
                "horizon",
                "health",
                "parent",
                "area",
            )
        )
    storage = "  source: Items\n  format_version: 1\n"
    if layout == "markdown-log":
        storage = """  source: Log.md
  format_version: 1
  section: {title: Entries, level: 2}
  item: {level: 3, key: record_id}
  fields:
    title: {kind: bullet, label: Title}
    count: {kind: bullet, label: Count}
"""
    return f"""---
type: collection
exomem_id: {CID}
title: Work
semantic_profile: {profile}
collection_version: 1
schema_version: 1
lifecycle: active
storage:
  strategy: {layout}
{storage}item_schema:
  natural_key: [title]
  fields:
{fields}---
"""


def manifest_path(profile="records"):
    return f"Knowledge Base/{profile.title()}/Work/_collection.md"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("EXOMEM_COLLECTION_STORE_PREVIEW", "1")
    from exomem.collection_store.writer import CollectionWriter

    root = tmp_path / "vault"
    (root / "Knowledge Base").mkdir(parents=True)
    (root / "Knowledge Base/log.md").write_text("# Existing log\n")
    with connection.open_writer(
        tmp_path / "collections.sqlite", lease_check=lambda: True
    ) as handle:
        yield CollectionWriter(root, handle)


def create(store, profile="records"):
    return store.create_collection(
        manifest_path(profile), manifest_text(profile), why="capture", scaffold=False
    )


@pytest.mark.parametrize("profile", ["records", "planning"])
def test_append_replay_conflict_and_closed_receipts(store, profile):
    receipt = create(store, profile)
    valid = (
        mutation_terminal.valid_record_receipt
        if profile == "records"
        else mutation_terminal.valid_planning_receipt
    )
    assert valid(receipt) and mutation_terminal.valid_collection_receipt(receipt)
    first = store.append_record(CID, item={"title": "One"}, item_key=KEY, why="observe")
    assert valid(first) and mutation_terminal.valid_collection_receipt(first)
    replay = store.append_record(CID, item={"title": "One"}, item_key=KEY, why="repeat")
    assert replay["outcome"] == "replayed" and valid(replay)
    assert replay["audit_correlation"] == first["audit_correlation"]
    with pytest.raises(collections.CollectionError, match="RECORD_ID_CONFLICT"):
        store.append_record(CID, item={"title": "Different"}, item_key=KEY, why="conflict")
    with pytest.raises(collections.CollectionError, match="RECORD_NATURAL_KEY_CONFLICT") as error:
        store.append_record(CID, item={"title": "One"}, item_key=OTHER, why="conflict")
    assert error.value.details["item_keys"] == [KEY]
    assert store.connection.execute("SELECT COUNT(*) FROM txns").fetchone()[0] == 2


@pytest.mark.parametrize("profile", ["records", "planning"])
def test_update_stale_guards_and_version_history(store, profile):
    create(store, profile)
    first = store.append_record(
        CID, item={"title": "One"}, item_key=KEY, body="Authored body\n", why="observe"
    )
    args = dict(
        item_key=KEY,
        changes={"title": "Two"},
        expected_container_hash=first["after_container_hash"],
        expected_item_version=first["after_item_hash"],
        why="correct",
    )
    with pytest.raises(collections.CollectionError, match="STALE_RECORD.*container"):
        store.update_record(CID, **{**args, "expected_container_hash": "0" * 64})
    with pytest.raises(collections.CollectionError, match="STALE_RECORD.*item"):
        store.update_record(CID, **{**args, "expected_item_version": "0" * 64})
    result = store.update_record(CID, **args)
    assert mutation_terminal.valid_collection_receipt(result)
    assert result["before_item_hash"] == first["after_item_hash"]
    assert result["after_item_hash"] != first["after_item_hash"]
    assert store.connection.execute("SELECT row_version, body FROM items").fetchone() == (
        2,
        "Authored body\n",
    )
    assert store.connection.execute("SELECT COUNT(*) FROM item_versions").fetchone()[0] == 2
    assert store.connection.execute("SELECT generation FROM collections").fetchone()[0] == 3


def test_planning_hierarchy_and_triage_are_validated_inside_transaction(store):
    create(store, "planning")
    first = store.append_record(CID, item={"title": "Child"}, item_key=KEY, why="capture")
    args = dict(
        item_key=KEY,
        changes={"status": "planned", "commitment": "committed", "horizon": "week"},
        operation="triage",
        expected_container_hash=first["after_container_hash"],
        expected_item_version=first["after_item_hash"],
        why="commit",
    )
    with pytest.raises(collections.CollectionError, match="INVALID_PLAN"):
        store.update_record(CID, **args)
    assert store.connection.execute("SELECT COUNT(*) FROM txns").fetchone()[0] == 2
    triaged = store.update_record(
        CID,
        **{
            **args,
            "changes": {"status": "planned", "commitment": "considering", "horizon": "week"},
        },
    )
    assert triaged["operation"] == "triage" and mutation_terminal.valid_planning_receipt(triaged)
    with pytest.raises(collections.CollectionError, match="INVALID_PLAN_ARGUMENTS"):
        store.update_record(CID, **{**args, "changes": {"title": "Bad triage"}})


@pytest.mark.parametrize("profile", ["records", "planning"])
def test_revise_is_guarded_and_keeps_manifest_history(store, profile):
    made = create(store, profile)
    before = tokens.manifest_hash(manifest_text(profile))
    proposed = manifest_text(profile).replace("title: Work", "title: Revised")
    receipt = store.revise_collection(
        CID,
        manifest_text=proposed,
        expected_manifest_hash=before,
        expected_container_hash=made["after_container_hash"],
        why="revise",
    )
    assert mutation_terminal.valid_collection_receipt(receipt)
    assert receipt["before_manifest_hash"] == before
    assert receipt["after_manifest_hash"] == tokens.manifest_hash(proposed)
    assert receipt["continuity"] is True
    assert store.connection.execute("SELECT COUNT(*) FROM collection_manifests").fetchone()[0] == 2
    with pytest.raises(collections.CollectionError, match="STALE_RECORD"):
        store.revise_collection(
            CID,
            manifest_text=proposed,
            expected_manifest_hash=before,
            expected_container_hash=made["after_container_hash"],
            why="stale",
        )


def test_held_resume_discard_do_not_advance_generation_or_audit(store):
    made = create(store)
    with pytest.raises(collections.CollectionError, match="SCHEMA_FIELD_TYPE") as error:
        store.append_record(CID, item={"title": "One", "count": "invalid"}, why="capture")
    held = error.value.details["held"]["held_id"]
    assert store.connection.execute("SELECT generation FROM collections").fetchone()[0] == 1
    assert chain.verify_store_chain(store.connection)[0] == 1
    assert store.connection.execute("SELECT COUNT(*) FROM held_candidates").fetchone()[0] == 1
    result = store.append_record(
        CID,
        held=held,
        item={"count": 1},
        expected_container_hash=made["after_container_hash"],
        why="correct",
    )
    assert mutation_terminal.valid_record_receipt(result)
    assert store.connection.execute("SELECT COUNT(*) FROM held_candidates").fetchone()[0] == 0
    assert (
        store.connection.execute(
            "SELECT effect FROM audit_effects ORDER BY txn_id DESC LIMIT 1"
        ).fetchone()[0]
        == "resume"
    )
    with pytest.raises(collections.CollectionError) as error:
        store.append_record(CID, item={"title": "Two", "count": "invalid"}, why="capture")
    held = error.value.details["held"]["held_id"]
    before = chain.recorded_head(store.connection)
    receipt = store.discard_held(CID, held=held, why="discard")
    assert mutation_terminal.valid_record_receipt(receipt)
    assert chain.recorded_head(store.connection) == before


def test_commit_survives_lost_transport_ledger_and_retry_after_reopen(store):
    create(store)
    args = dict(item={"title": "One"}, item_key=KEY, why="observe", request_id="lost-ledger")
    receipt = store.append_record(CID, **args)
    path, root = store.handle.path, store.root
    stored = store.connection.execute(
        "SELECT receipt_json FROM txns WHERE request_id = ?", ("lost-ledger",)
    ).fetchone()[0]
    assert json.loads(stored) == receipt
    store.handle.close()
    from exomem.collection_store.writer import CollectionWriter

    with connection.open_writer(path, lease_check=lambda: True) as handle:
        reopened = CollectionWriter(root, handle)
        assert reopened.append_record(CID, **args) == receipt
        assert reopened.connection.execute("SELECT COUNT(*) FROM txns").fetchone()[0] == 2
        with pytest.raises(collections.CollectionError, match="IDEMPOTENCY_KEY_REUSED"):
            reopened.append_record(CID, **{**args, "item": {"title": "Different"}})
        assert chain.verify_store_chain(reopened.connection)[0] == 2


def test_one_immediate_transaction_txns_last_and_atomic_rollback(store, monkeypatch):
    create(store)
    statements = []
    store.connection.set_trace_callback(statements.append)
    store.append_record(CID, item={"title": "One"}, why="observe")
    assert sum(sql == "BEGIN IMMEDIATE" for sql in statements) == 1
    assert sum(sql == "COMMIT" for sql in statements) == 1
    tables = [sql.split()[2].split("(")[0] for sql in statements if sql.startswith("INSERT INTO ")]
    assert tables[-1] == "txns"
    assert {"items", "item_versions", "audit_effects", "projection_state"} <= set(tables)
    before = chain.recorded_head(store.connection)

    def fail(*_args, **_kwargs):
        raise RuntimeError("fail before txn insert")

    monkeypatch.setattr(store, "_insert_txn", fail)
    with pytest.raises(RuntimeError, match="fail before txn insert"):
        store.append_record(CID, item={"title": "Two"}, why="observe")
    assert chain.recorded_head(store.connection) == before
    assert store.connection.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1
    assert store.connection.execute("SELECT COUNT(*) FROM item_versions").fetchone()[0] == 1
    assert store.connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_sources_and_pending_projection_commit_with_item_without_file_writes(store):
    create(store)
    source = "Knowledge Base/Sources/sample.md"
    (store.root / source).parent.mkdir(parents=True)
    (store.root / source).write_text("---\ntype: source\n---\nSample\n")
    result = store.append_record(CID, item={"title": "One"}, why="observe", sources=(source,))
    assert store.connection.execute("SELECT source_ref FROM item_sources").fetchall() == [(source,)]
    path, version, digest, state = store.connection.execute(
        "SELECT path, pending_row_version, pending_sha256, state FROM projection_state WHERE kind = 'item'"
    ).fetchone()
    assert (
        path == result["affected_paths"][0]
        and version == 1
        and len(digest) == 64
        and state == "pending"
    )
    assert not (store.root / path).exists()
    assert (store.root / "Knowledge Base/log.md").read_text() == "# Existing log\n"
    with closing(connection.open_reader(store.handle.path)) as reader:
        assert reader.execute("SELECT COUNT(*) FROM item_sources").fetchone()[0] == 1


def _race_worker(path, root, key, ready, go, output):
    from exomem.collection_store.writer import CollectionWriter

    with connection.open_writer(Path(path), lease_check=lambda: True) as handle:
        ready.put(True)
        go.wait(10)
        try:
            result = CollectionWriter(Path(root), handle).append_record(
                CID, item={"title": "Race"}, item_key=key, why="race"
            )
            output.put(result["outcome"])
        except collections.CollectionError as error:
            output.put(error.code)


def test_two_process_writers_natural_key_race_is_serialized(store):
    create(store)
    path, root = store.handle.path, store.root
    store.handle.close()
    ctx = multiprocessing.get_context("spawn")
    ready, output, go = ctx.Queue(), ctx.Queue(), ctx.Event()
    workers = [
        ctx.Process(target=_race_worker, args=(str(path), str(root), key, ready, go, output))
        for key in (KEY, OTHER)
    ]
    try:
        for worker in workers:
            worker.start()
        for _ in workers:
            assert ready.get(timeout=20)
        go.set()
        results = [output.get(timeout=20) for _ in workers]
        for worker in workers:
            worker.join(timeout=20)
            assert worker.exitcode == 0
        assert sorted(results) == ["RECORD_NATURAL_KEY_CONFLICT", "committed"]
        with closing(connection.open_reader(path)) as conn:
            assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1
            assert chain.verify_store_chain(conn)[0] == 2
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join()
        ready.close()
        output.close()


def test_writer_is_dark_without_preview_flag(tmp_path, monkeypatch):
    from exomem.collection_store.writer import CollectionWriter

    monkeypatch.delenv("EXOMEM_COLLECTION_STORE_PREVIEW", raising=False)
    with connection.open_writer(
        tmp_path / "collections.sqlite", lease_check=lambda: True
    ) as handle:
        with pytest.raises(
            connection.CollectionStoreError, match="COLLECTION_STORE_PREVIEW_REQUIRED"
        ):
            CollectionWriter(tmp_path, handle)


def test_builtin_store_writer_is_available():
    import importlib.util

    assert importlib.util.find_spec("exomem.collection_store.writer") is not None, (
        "built-in store writer is missing"
    )


def test_log_pending_hash_is_the_actual_rendered_container(store):
    import hashlib

    text = (
        manifest_text()
        .replace(
            "strategy: markdown-items\n  source: Items",
            """strategy: markdown-log
  source: Log.md""",
        )
        .replace(
            "  format_version: 1\n",
            """  format_version: 1
  section: {level: 2, title: Entries}
  item_heading:
    level: 3
    fields: [{name: title, type: string}]
    separator: " · "
  child_rows:
    prefix: "- "
    delimiter: "|"
    fields: [field, value]
    container_field: details
  insertion: newest-first
""",
        )
        .replace("    count: {type: integer}", "    details: {type: array, items: {type: object}}")
    )
    store.create_collection(manifest_path(), text, why="create")
    values = {"title": "One", "details": []}
    receipt = store.append_record(CID, item=values, item_key=KEY, why="capture")
    manifest = collections.parse_manifest_bytes(store.root, manifest_path(), text.encode())
    from exomem import record_formats

    expected = "## Entries\n" + record_formats.render_markdown_log_item(manifest, values, KEY, "\n")
    digest = store.connection.execute(
        "SELECT pending_sha256 FROM projection_state WHERE kind = 'log'"
    ).fetchone()[0]
    assert digest == hashlib.sha256(expected.encode()).hexdigest()
    assert receipt["affected_paths"] == [manifest.storage.source]
    assert (
        store.connection.execute("SELECT view_path FROM items").fetchone()[0]
        == f"{manifest.storage.source}#{KEY}"
    )


def test_hold_pending_hash_matches_rendered_held_bytes(store):
    import hashlib

    create(store)
    with pytest.raises(collections.CollectionError):
        store.append_record(CID, item={"title": "Bad", "count": "invalid"}, why="capture")
    text = store.connection.execute("SELECT held_bytes FROM held_candidates").fetchone()[0]
    digest = store.connection.execute(
        "SELECT pending_sha256 FROM projection_state WHERE kind = 'held'"
    ).fetchone()[0]
    assert text is not None and text.startswith(b"---\n")
    assert digest == hashlib.sha256(text).hexdigest()


def test_provenance_is_rechecked_at_precommit(store, monkeypatch):
    from exomem import vault
    from exomem.collection_store import writer as writer_module

    create(store)
    source = "Knowledge Base/Sources/sample.md"
    (store.root / source).parent.mkdir(parents=True)
    (store.root / source).write_text("Sample\n")

    def change_source(*args):
        (store.root / source).write_text("Changed\n")

    monkeypatch.setattr(writer_module, "governance_precommit_pending", change_source)
    with pytest.raises(vault.PathGuardError):
        store.append_record(CID, item={"title": "One"}, why="capture", sources=(source,))
    assert store.connection.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
    assert chain.verify_store_chain(store.connection)[0] == 1
