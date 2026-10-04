"""Captured legacy state is proved before an unpublished transaction can commit."""

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import replace
from uuid import UUID

import pytest
from record_fixtures import copy_dataset_fixture, copy_x3_fixture
from test_collection_store_legacy import _event, _rewrite
from test_collection_store_writer import CID, KEY, manifest_path, manifest_text
from test_collection_store_writer import store as store
from test_governance_egress import _external, write_rule, write_scope

from exomem import held_fs, record_formats, records, vault
from exomem import structured_collections as collections
from exomem.collection_store import chain, legacy, legacy_import, schema
from exomem.collection_store.connection import CollectionStoreError
from exomem.governance.principal import request_scope

CONTEXT = legacy_import.ImportContext("migration-test", "2026-10-02T10:00:00Z", "attempt-one")


def _items(tmp_path, *, profile="records", text=None, values=None, body="Authored body\n"):
    root = tmp_path / "vault"
    path = root / manifest_path(profile)
    path.parent.mkdir(parents=True)
    (root / "Knowledge Base/log.md").write_text("# Activity\n")
    path.write_text(text or manifest_text(profile))
    manifest = collections.load_manifest(root, path)
    source = root / manifest.storage.source
    source.mkdir(exist_ok=True)
    if values is not None:
        (source / "Original.md").write_text(
            record_formats.render_markdown_item(manifest, values, KEY, body)
        )
    return root, path


@contextmanager
def _capture(tmp_path, root, path):
    stage = tmp_path / "stage"
    stage.mkdir(exist_ok=True, mode=0o700)
    with legacy.LegacyAuditSpool(stage, deadline=time.monotonic() + 60) as audit:
        audit.scan(root)
        yield audit, legacy_import.capture_legacy_collection(root, path, audit=audit)


def _connection():
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("PRAGMA foreign_keys=ON")
    schema.ensure_schema(conn)
    return conn


def _held(path, number, candidate, *, target_key=None):
    held_id = str(UUID(int=(4 << 76) | (2 << 62) | number))
    body = json.dumps(
        records._encode_held_value(candidate), ensure_ascii=False, sort_keys=True, allow_nan=False
    )
    data = {
        "type": "held-record",
        "collection_id": CID,
        "held_id": held_id,
        "attempted_action": "append",
        "held_at": CONTEXT.imported_at,
        "why": "invented refusal",
        "candidate_sha256": hashlib.sha256(body.encode()).hexdigest(),
        "diagnostics": "[]",
    }
    if target_key is not None:
        data["target_item_key"] = target_key
    raw = (
        "---\n" + vault.serialize_frontmatter(data) + "\n---\n\n```json\n" + body + "\n```\n"
    ).encode()
    target = path.parent / "Held" / (held_id + ".md")
    target.parent.mkdir(exist_ok=True)
    target.write_bytes(raw)
    return held_id, raw


def test_baseline_checkpoint_preserves_current_rows_and_exact_projections(tmp_path):
    # Without a checkpoint, callers could commit a successful-looking empty import.
    root, path = _items(tmp_path, values={"title": "One", "count": 3}, body="\n\nAuthored body\n")
    original = (root / "Knowledge Base/Records/Work/Items/Original.md").read_bytes()
    with _capture(tmp_path, root, path) as (audit, captured), _connection() as conn:
        conn.execute("BEGIN")
        result = legacy_import.import_legacy_collection(
            conn, captured, audit=audit, context=CONTEXT
        )
        row = conn.execute(
            "SELECT generation,legacy_audit_status,verified_through_txn FROM collections"
        ).fetchone()
        assert row is not None
        assert row == (1, "baseline", result.checkpoint_txn_id)
        assert conn.execute(
            "SELECT item_key,values_json,body,row_version FROM items"
        ).fetchone() == (KEY, '{"count":3,"title":"One"}', "\n\nAuthored body\n", 1)
        assert conn.execute("SELECT COUNT(*) FROM item_versions").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM item_sources").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM audit_effects").fetchone()[0] == 0
        assert conn.execute(
            "SELECT published_sha256,state,pending_sha256,install_json FROM projection_state WHERE kind='item'"
        ).fetchone() == (hashlib.sha256(original).hexdigest(), "current", None, None)
        assert (
            conn.execute("SELECT manifest_text FROM collection_manifests").fetchone()[0].encode()
            == captured.manifest_bytes
        )
        assert chain.verify_store_chain(conn)[0] == 1


def test_complete_held_capture_and_codec_are_not_limited_to_500(tmp_path):
    # The old inspector silently omits the 501st held candidate.
    root, path = _items(tmp_path)
    for number in range(1, 502):
        held_id, raw = _held(
            path, number, {"action": "append", "item": {"title": "Held"}, "invalid": float("nan")}
        )
    with _capture(tmp_path, root, path) as (audit, captured), _connection() as conn:
        assert len(captured.held) == 501
        conn.execute("BEGIN")
        legacy_import.import_legacy_collection(conn, captured, audit=audit, context=CONTEXT)
        assert conn.execute("SELECT COUNT(*) FROM held_candidates").fetchone()[0] == 501
        saved, code, candidate = conn.execute(
            "SELECT held_bytes,code,candidate_json FROM held_candidates WHERE held_id=?", (held_id,)
        ).fetchone()
        assert saved == raw and code is None
        assert (
            records._decode_held_value(json.loads(candidate))["invalid"]
            != records._decode_held_value(json.loads(candidate))["invalid"]
        )


@pytest.mark.parametrize("gap", [False, True])
def test_history_envelopes_keep_original_json_and_do_not_bless_a_gap(tmp_path, gap, store):
    # A valid store chain must not replace legacy provenance or reserialize events.
    root, path = _items(tmp_path, values={"title": "One"})
    path.write_text(
        record_formats.render_manifest_audit_head(
            path.read_text(), f"{3:024x}", semantic_profile="records", reader_version=1
        )
    )
    manifest = collections.load_manifest(root, path)
    snapshot = record_formats.load_adapter(root, manifest).read()
    events = [_event(manifest, n) for n in (1, 2, 3)]
    events[-1] = _rewrite(
        events[-1],
        after_container_hash=snapshot.snapshot,
        after_manifest_hash="f" * 64 if gap else manifest.manifest_version.hash,
    )
    prefix = records.profile_for("records").activity_prefix
    originals = [line[len(prefix) :] for line in events]
    if gap:
        # This orphan influences the report but is not in the imported parent chain.
        events.append(_event(manifest, 4))
    (root / "Knowledge Base/log.md").write_text("\n".join(reversed(events)) + "\n")
    with _capture(tmp_path, root, path) as (audit, captured):
        conn = store.connection
        assert captured.legacy_inspection["status"] == ("gap" if gap else "ok")
        conn.execute("BEGIN")
        result = legacy_import.import_legacy_collection(
            conn, captured, audit=audit, context=CONTEXT
        )
        rows = conn.execute(
            "SELECT transition_id,legacy_event_json,generation_before,generation_after,manifest_version_before,manifest_version_after,actor,committed_at FROM txns ORDER BY txn_id"
        ).fetchall()
        assert [row[1] for row in rows[:-1]] == originals
        assert [row[0] for row in rows[:-1]] == [f"{n:024x}" for n in (1, 2, 3)]
        assert [row[2:6] for row in rows] == [
            (0, 1, None, None),
            (1, 2, None, None),
            (2, 3, None, None),
            (3, 4, None, 1),
        ]
        assert all(row[6:] == (CONTEXT.actor, CONTEXT.imported_at) for row in rows)
        assert rows[-1][1] is None
        checkpoint = json.loads(
            conn.execute(
                "SELECT receipt_json FROM txns WHERE legacy_event_json IS NULL"
            ).fetchone()[0]
        )
        assert checkpoint["bounded_inspection"] == captured.legacy_inspection
        assert (
            checkpoint["exhaustive_assessment"]["status"] == captured.audit_proof.exhaustive_status
        )
        assert checkpoint["original_history"] == {
            "manifest_head": captured.manifest.audit_head,
            "reachable_head": captured.audit_proof.reachable_head,
            "count": 3,
            "ordered_event_digest": captured.audit_proof.ordered_event_digest,
        }
        assert checkpoint["verification_summary"] == dict.fromkeys("abcdef", "passed")
        assert conn.execute(
            "SELECT legacy_audit_status,audit_head,verified_through_txn FROM collections"
        ).fetchone() == (
            "gap" if gap else "ok",
            result.checkpoint_event_hash,
            result.checkpoint_txn_id,
        )
        assert chain.verify_store_chain(conn)[0] == 4
        conn.commit()
        assert store.inspect_collection(CID)["audit"] == captured.legacy_inspection
        if gap:
            write_scope(root, paths="Records/**/4.md")
            write_rule(root, ceiling=0)
            with request_scope(_external()):
                assert store.inspect_collection(CID)["audit"] == {
                    "status": "history_incomplete", "gaps": []
                }


def test_capture_cannot_mix_another_vaults_identical_current_state_with_scanned_history(tmp_path):
    # Equal manifests/rows/head do not authorize substituting another vault's original events.
    inputs = []
    for name in ("first", "second"):
        directory = tmp_path / name
        directory.mkdir()
        root, path = _items(directory, values={"title": "One"})
        path.write_text(
            record_formats.render_manifest_audit_head(
                path.read_text(),
                f"{1:024x}",
                semantic_profile="records",
                reader_version=1,
            )
        )
        manifest = collections.load_manifest(root, path)
        snapshot = record_formats.load_adapter(root, manifest).read()
        event = _rewrite(
            _event(manifest, 1, why=f"history from {name}"), after_container_hash=snapshot.snapshot
        )
        (root / "Knowledge Base/log.md").write_text(event + "\n")
        inputs.append((root, path, snapshot.snapshot))
    first_root, first_path, first_hash = inputs[0]
    second_root, second_path, second_hash = inputs[1]
    assert first_path.read_bytes() == second_path.read_bytes() and first_hash == second_hash
    assert (first_root / "Knowledge Base/log.md").read_bytes() != (
        second_root / "Knowledge Base/log.md"
    ).read_bytes()
    stage = tmp_path / "stage"
    stage.mkdir(mode=0o700)
    with legacy.LegacyAuditSpool(stage, deadline=time.monotonic() + 60) as audit:
        audit.scan(first_root)
        with pytest.raises(CollectionStoreError, match="COLLECTION_LEGACY_AUDIT_ROOT_MISMATCH"):
            legacy_import.capture_legacy_collection(second_root, second_path, audit=audit)


def test_import_revalidates_captured_root_before_mapping(tmp_path):
    # A caller cannot redirect a valid capture's guards to another physical vault.
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    root, path = _items(first, values={"title": "One"})
    other_root, _ = _items(second, values={"title": "One"})
    with _capture(first, root, path) as (audit, captured), _connection() as conn:
        with pytest.raises(CollectionStoreError, match="COLLECTION_LEGACY_AUDIT_ROOT_MISMATCH"):
            with conn:
                conn.execute("BEGIN")
                legacy_import.import_legacy_collection(
                    conn, replace(captured, root=other_root), audit=audit, context=CONTEXT
                )
        assert conn.execute("SELECT COUNT(*) FROM txns").fetchone()[0] == 0


def test_physical_source_root_alias_can_be_captured_and_imported(tmp_path):
    # Lexical root comparisons would reject a safe alias of the same physical anchor.
    root, path = _items(tmp_path, values={"title": "One"})
    alias = root / ".." / root.name
    with _capture(tmp_path, root, path) as (audit, _), _connection() as conn:
        captured = legacy_import.capture_legacy_collection(alias, manifest_path(), audit=audit)
        conn.execute("BEGIN")
        result = legacy_import.import_legacy_collection(
            conn, captured, audit=audit, context=CONTEXT
        )
        assert (
            conn.execute("SELECT verified_through_txn FROM collections").fetchone()[0]
            == result.checkpoint_txn_id
        )


def test_log_capture_preserves_full_frame_and_adapter_order(tmp_path):
    # Newest-first row allocation must not invert historical order or lose suffix.
    root = tmp_path / "vault"
    path = copy_x3_fixture(root) / "_collection.md"
    (root / "Knowledge Base/log.md").write_text("# Activity\n")
    source = path.parent / "Training Log.md"
    original = (
        b"\xef\xbb\xbf"
        + source.read_bytes().replace(b"\n", b"\r\n")
        + b"\r\n## Outside\r\nAuthored suffix"
    )
    source.write_bytes(original)
    with _capture(tmp_path, root, path) as (audit, captured), _connection() as conn:
        conn.execute("BEGIN")
        legacy_import.import_legacy_collection(conn, captured, audit=audit, context=CONTEXT)
        keys = conn.execute(
            "SELECT item_key FROM items ORDER BY created_txn DESC,row_id DESC"
        ).fetchall()
        assert [row[0] for row in keys] == [row.identity.key for row in captured.snapshot.records]
        frame = json.loads(conn.execute("SELECT log_frame_json FROM collections").fetchone()[0])
        assert frame["bom"] and frame["newline"] == "\r\n" and not frame["final_newline"]
        assert frame["suffix"].encode() == original[original.index(b"## Current") :]
        assert frame["text"].encode() == captured.log_frame.prefix
        assert (
            conn.execute(
                "SELECT published_sha256 FROM projection_state WHERE kind='log'"
            ).fetchone()[0]
            == hashlib.sha256(original).hexdigest()
        )


def test_planning_uses_complete_hierarchy_without_append_defaults(tmp_path):
    # Importing a dangling parent or silently supplying defaults changes authored intent.
    values = {
        "title": "Plan",
        "kind": "work-item",
        "status": "candidate",
        "lifecycle": "active",
        "priority": "none",
        "commitment": "uncommitted",
        "horizon": "inbox",
        "health": "unknown",
    }
    root, path = _items(tmp_path, profile="planning", values=values)
    path.write_text(
        record_formats.render_manifest_audit_head(
            path.read_text(), f"{7:024x}", semantic_profile="planning", reader_version=1
        )
    )
    manifest = collections.load_manifest(root, path)
    snapshot = record_formats.load_adapter(root, manifest).read()
    event = _rewrite(
        _event(manifest, 1), transition_id=f"{7:024x}", after_container_hash=snapshot.snapshot
    )
    (root / "Knowledge Base/log.md").write_text(event + "\n")
    with _capture(tmp_path, root, path) as (audit, captured), _connection() as conn:
        conn.execute("BEGIN")
        legacy_import.import_legacy_collection(conn, captured, audit=audit, context=CONTEXT)
        assert json.loads(conn.execute("SELECT values_json FROM items").fetchone()[0]) == values
        assert (
            conn.execute(
                "SELECT profile_operation FROM txns WHERE legacy_event_json IS NOT NULL"
            ).fetchone()[0]
            == "plan_add"
        )
    source = path.parent / "Items/Original.md"
    manifest = collections.load_manifest(root, path)
    source.write_text(
        record_formats.render_markdown_item(
            manifest,
            {**values, "parent": "exomem://plan/" + CID + "/22222222-2222-4222-8222-222222222222"},
            KEY,
        )
    )
    with _capture(tmp_path, root, path) as (audit, captured), _connection() as conn:
        with pytest.raises(collections.CollectionError, match="INVALID_PLAN"):
            with conn:
                conn.execute("BEGIN")
                legacy_import.import_legacy_collection(conn, captured, audit=audit, context=CONTEXT)
        assert conn.execute("SELECT COUNT(*) FROM txns").fetchone()[0] == 0


def test_managed_presentation_refuses_payload_check_c_and_rolls_back(tmp_path):
    # The unresolved shared-managed-body hash must not be blessed by import.
    text = manifest_text().replace(
        "item_schema:\n", "item_presentation:\n  version: 1\n  title: title\nitem_schema:\n"
    )
    root, path = _items(tmp_path, text=text, values={"title": "One"})
    before = (path.parent / "Items/Original.md").read_bytes()
    with _capture(tmp_path, root, path) as (audit, captured), _connection() as conn:
        with pytest.raises(CollectionStoreError, match=r"check\(c\).*payload_hash"):
            with conn:
                conn.execute("BEGIN")
                legacy_import.import_legacy_collection(conn, captured, audit=audit, context=CONTEXT)
        assert conn.execute("SELECT COUNT(*) FROM collections").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM txns").fetchone()[0] == 0
        assert (path.parent / "Items/Original.md").read_bytes() == before


@pytest.mark.parametrize("change", ["manifest", "item", "held-add"])
def test_changed_capture_refuses_before_commit(tmp_path, change):
    # No complete-capture claim survives an edited source or new held candidate.
    root, path = _items(tmp_path, values={"title": "One"})
    with _capture(tmp_path, root, path) as (audit, captured), _connection() as conn:
        if change == "held-add":
            _held(path, 1, {"action": "append", "item": {"title": "Held"}})
        else:
            target = path if change == "manifest" else path.parent / "Items/Original.md"
            target.write_bytes(target.read_bytes() + b"\nChanged\n")
        with pytest.raises(CollectionStoreError, match="SOURCE_CHANGED"):
            with conn:
                conn.execute("BEGIN")
                legacy_import.import_legacy_collection(conn, captured, audit=audit, context=CONTEXT)
        assert conn.execute("SELECT COUNT(*) FROM txns").fetchone()[0] == 0


def test_import_context_is_deterministic_and_requires_caller_transaction(tmp_path):
    # Per-event clocks or random checkpoint IDs make identical imports disagree.
    root, path = _items(tmp_path, values={"title": "One"})
    with _capture(tmp_path, root, path) as (audit, captured):
        transactions = []
        for _ in range(2):
            with _connection() as conn:
                with pytest.raises(RuntimeError, match="caller transaction"):
                    legacy_import.import_legacy_collection(
                        conn, captured, audit=audit, context=CONTEXT
                    )
                conn.execute("BEGIN")
                legacy_import.import_legacy_collection(conn, captured, audit=audit, context=CONTEXT)
                transactions.append(
                    conn.execute(
                        "SELECT transition_id,receipt_json,event_hash,store_head_hash FROM txns"
                    ).fetchall()
                )
        assert transactions[0] == transactions[1] and transactions[0]
    with pytest.raises(ValueError):
        replace(CONTEXT, imported_at="2026-10-02T10:00:00.123Z")


@pytest.mark.parametrize("profile", ["records", "planning"])
def test_acknowledged_rebaseline_keeps_status_and_captured_reader_version(tmp_path, store, profile):
    # A healthy checkpoint cannot erase acknowledged discontinuity or reader2.
    root, path = _items(tmp_path, profile=profile)
    path.write_text(
        record_formats.render_manifest_audit_head(
            path.read_text(), f"{2:024x}", semantic_profile=profile, reader_version=2
        )
    )
    manifest = collections.load_manifest(root, path)
    snapshot = record_formats.load_adapter(root, manifest).read()
    event = records._lifecycle_audit_body(
        transition_id=f"{2:024x}",
        parent_id=f"{1:024x}",
        operation="rebaseline",
        manifest=manifest,
        before_manifest_hash=manifest.manifest_version.hash,
        after_manifest_hash=manifest.manifest_version.hash,
        before_container_hash="1" * 64,
        after_container_hash=snapshot.snapshot,
        payload_hash="2" * 64,
        why="acknowledge invented gap",
        continuity=False,
        acknowledged_gap_codes=("current-container-mismatch",),
        gap_fingerprint="3" * 64,
        checkpoint_snapshot_hash="4" * 64,
    )
    (root / "Knowledge Base/log.md").write_text(event + "\n" + _event(manifest, 1) + "\n")
    with _capture(tmp_path, root, path) as (audit, captured):
        conn = store.connection
        assert captured.legacy_inspection["status"] == "acknowledged_gap"
        conn.execute("BEGIN")
        legacy_import.import_legacy_collection(conn, captured, audit=audit, context=CONTEXT)
        assert conn.execute(
            "SELECT legacy_audit_status,audit_reader_version FROM collections"
        ).fetchone() == ("acknowledged_gap", 2)
        conn.commit()
        assert store.inspect_collection(CID)["audit"] == captured.legacy_inspection
        # A deleted historical item still gates gap topology and private rationale.
        write_scope(root, paths=f"{profile.title()}/**/1.md")
        write_rule(root, ceiling=0)
        with request_scope(_external()):
            assert store.inspect_collection(CID)["audit"] == {
                "status": "history_incomplete", "gaps": []
            }


def test_duplicate_complete_key_refuses_without_overwriting_rows(tmp_path):
    # Distinct IDs with the same complete natural key are not two importable rows.
    root, path = _items(tmp_path, values={"title": "One"})
    manifest = collections.load_manifest(root, path)
    (path.parent / "Items/Other.md").write_text(
        record_formats.render_markdown_item(
            manifest, {"title": "One"}, "22222222-2222-4222-8222-222222222222"
        )
    )
    with _capture(tmp_path, root, path) as (audit, captured), _connection() as conn:
        with pytest.raises(CollectionStoreError, match="NATURAL_KEY_CONFLICT"):
            with conn:
                conn.execute("BEGIN")
                legacy_import.import_legacy_collection(conn, captured, audit=audit, context=CONTEXT)
        assert conn.execute("SELECT COUNT(*) FROM collections").fetchone()[0] == 0


@pytest.mark.parametrize("defect", ["duplicate-id", "invalid-schema", "dataset"])
def test_unrepresentable_source_refuses_capture(tmp_path, defect):
    # Capture must not launder invalid source through normalized canonical rows.
    if defect == "dataset":
        root = tmp_path / "vault"
        path = copy_dataset_fixture(root) / "_collection.md"
        (root / "Knowledge Base/log.md").write_text("# Activity\n")
        expected = "UNSUPPORTED"
    else:
        root, path = _items(tmp_path, values={"title": "One"})
        source = path.parent / "Items/Original.md"
        if defect == "duplicate-id":
            (source.parent / "Copy.md").write_bytes(source.read_bytes())
            expected = "DUPLICATE_ID"
        else:
            source.write_text(source.read_text().replace("title: One", "count: invalid"))
            expected = "SCHEMA_REQUIRED_FIELD"
    with pytest.raises((CollectionStoreError, collections.CollectionError), match=expected):
        with _capture(tmp_path, root, path):
            pass


def test_manifest_and_held_ordinary_census_remain_exact(tmp_path):
    # CRLF and noncandidate directory entries must survive complete capture.
    root, path = _items(tmp_path)
    raw = path.read_bytes().replace(b"\n", b"\r\n").removesuffix(b"\r\n")
    path.write_bytes(raw)
    held_id, held_bytes = _held(path, 1, {"action": "append", "item": {"title": "Held"}})
    (path.parent / "Held/ordinary-directory").mkdir()
    (path.parent / "Held/readme.txt").write_text("ordinary held context")
    with _capture(tmp_path, root, path) as (audit, captured), _connection() as conn:
        assert len(captured.held_guard.entries) == 3 and len(captured.held) == 1
        conn.execute("BEGIN")
        legacy_import.import_legacy_collection(conn, captured, audit=audit, context=CONTEXT)
        assert (
            conn.execute("SELECT manifest_text FROM collection_manifests").fetchone()[0].encode()
            == raw
        )
        assert (
            conn.execute(
                "SELECT held_bytes FROM held_candidates WHERE held_id=?", (held_id,)
            ).fetchone()[0]
            == held_bytes
        )


def test_bom_manifest_retains_existing_named_refusal(tmp_path):
    # Capture cannot convert bytes the existing manifest parser rejects into a valid source.
    root, path = _items(tmp_path)
    raw = b"\xef\xbb\xbf" + path.read_bytes()
    path.write_bytes(raw)
    with pytest.raises(collections.CollectionError, match="INVALID_COLLECTION_MANIFEST"):
        with _capture(tmp_path, root, path):
            pass
    assert path.read_bytes() == raw


def test_changed_source_during_mapping_and_corrupt_stored_body_roll_back(tmp_path):
    # Preflight alone misses edits during SQL work; constraints alone miss body corruption.
    root, path = _items(tmp_path, values={"title": "One"})
    with _capture(tmp_path, root, path) as (audit, captured):
        for failure in ("stored-body", "source-change"):
            with _connection() as conn:
                if failure == "stored-body":
                    conn.execute(
                        "CREATE TEMP TRIGGER changed_body AFTER INSERT ON items BEGIN UPDATE items SET body='corrupt' WHERE row_id=NEW.row_id; END"
                    )
                    expected = r"check\(c\).*payload_hash"
                else:

                    def edit_source():
                        source = path.parent / "Items/Original.md"
                        source.write_bytes(source.read_bytes() + b"\nConcurrent edit\n")
                        return 0

                    conn.create_function("edit_source", 0, edit_source)
                    conn.execute(
                        "CREATE TEMP TRIGGER changed_source AFTER INSERT ON items BEGIN SELECT edit_source(); END"
                    )
                    expected = "SOURCE_CHANGED"
                with pytest.raises(CollectionStoreError, match=expected):
                    with conn:
                        conn.execute("BEGIN")
                        legacy_import.import_legacy_collection(
                            conn, captured, audit=audit, context=CONTEXT
                        )
                assert conn.execute("SELECT COUNT(*) FROM txns").fetchone()[0] == 0
                assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0


def test_missing_checkpoint_cannot_receive_verified_watermark(tmp_path):
    # A suppressed SQL insert must not be mistaken for a proved checkpoint.
    root, path = _items(tmp_path)
    with _capture(tmp_path, root, path) as (audit, captured), _connection() as conn:
        conn.execute(
            "CREATE TEMP TRIGGER missing_checkpoint BEFORE INSERT ON txns WHEN NEW.legacy_event_json IS NULL BEGIN SELECT RAISE(IGNORE); END"
        )
        with pytest.raises(CollectionStoreError, match=r"check\(d\).*checkpoint"):
            with conn:
                conn.execute("BEGIN")
                legacy_import.import_legacy_collection(conn, captured, audit=audit, context=CONTEXT)
        assert conn.execute("SELECT COUNT(*) FROM collections").fetchone()[0] == 0


def test_existing_collection_and_cross_profile_transition_collisions_refuse(tmp_path):
    # Global event IDs may collide across profiles and must never be renamed.
    root, path = _items(tmp_path)
    with _capture(tmp_path, root, path) as (audit, captured), _connection() as conn:
        conn.execute("BEGIN")
        legacy_import.import_legacy_collection(conn, captured, audit=audit, context=CONTEXT)
        conn.commit()
        conn.execute("BEGIN")
        with pytest.raises(CollectionStoreError, match="IMPORT_EXISTS"):
            legacy_import.import_legacy_collection(conn, captured, audit=audit, context=CONTEXT)
        conn.rollback()
        assert conn.execute("SELECT COUNT(*) FROM txns").fetchone()[0] == 1
    # Different collection roots, one staging transaction and the same legacy ID.
    for profile, subdir in (("records", "observed"), ("planning", "intended")):
        fixture = tmp_path / subdir
        fixture.mkdir()
        root, path = _items(fixture, profile=profile)
        if profile == "planning":
            path.write_text(path.read_text().replace(CID, "33333333-3333-4333-8333-333333333333"))
        path.write_text(
            record_formats.render_manifest_audit_head(
                path.read_text(), f"{1:024x}", semantic_profile=profile, reader_version=1
            )
        )
        manifest = collections.load_manifest(root, path)
        snapshot = record_formats.load_adapter(root, manifest).read()
        event = _event(manifest, 1)
        event = _rewrite(event, after_container_hash=snapshot.snapshot)
        (root / "Knowledge Base/log.md").write_text(event + "\n")
    with _connection() as conn:
        with pytest.raises(CollectionStoreError, match="TRANSITION_CONFLICT"):
            with conn:
                conn.execute("BEGIN")
                for subdir, profile in (("observed", "records"), ("intended", "planning")):
                    fixture = tmp_path / subdir
                    root = fixture / "vault"
                    with _capture(fixture, root, root / manifest_path(profile)) as (
                        audit,
                        captured,
                    ):
                        legacy_import.import_legacy_collection(
                            conn, captured, audit=audit, context=CONTEXT
                        )
                        if profile == "records":
                            assert conn.execute("SELECT COUNT(*) FROM txns").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM collections").fetchone()[0] == 0


def test_global_source_path_collision_refuses_named_without_replacement(tmp_path):
    # A second collection cannot take an existing collection's projection path.
    roots = []
    for name, cid in (("one", CID), ("two", "33333333-3333-4333-8333-333333333333")):
        fixture = tmp_path / name
        fixture.mkdir()
        root, path = _items(fixture, text=manifest_text().replace(CID, cid))
        roots.append((fixture, root, path))
    with _connection() as conn:
        conn.execute("BEGIN")
        fixture, root, path = roots[0]
        with _capture(fixture, root, path) as (audit, captured):
            legacy_import.import_legacy_collection(conn, captured, audit=audit, context=CONTEXT)
        conn.commit()
        fixture, root, path = roots[1]
        with _capture(fixture, root, path) as (audit, captured):
            with pytest.raises(CollectionStoreError, match="PATH_CONFLICT"):
                with conn:
                    conn.execute("BEGIN")
                    legacy_import.import_legacy_collection(
                        conn, captured, audit=audit, context=CONTEXT
                    )
        assert conn.execute("SELECT collection_id FROM collections").fetchall() == [(CID,)]
        assert chain.verify_store_chain(conn)[0] == 1


def test_held_missing_update_target_cannot_widen_governance(tmp_path):
    # Inventing empty held metadata when the original target is missing widens release.
    root, path = _items(tmp_path)
    held_id, _ = _held(path, 1, {"action": "update", "changes": {"title": "Held"}})
    with _capture(tmp_path, root, path) as (audit, captured), _connection() as conn:
        assert captured.held[0].candidate.held_id == held_id
        with pytest.raises(CollectionStoreError, match="HELD_METADATA"):
            with conn:
                conn.execute("BEGIN")
                legacy_import.import_legacy_collection(conn, captured, audit=audit, context=CONTEXT)
        assert conn.execute("SELECT COUNT(*) FROM held_candidates").fetchone()[0] == 0


def test_held_update_inherits_actual_target_governance(tmp_path):
    # Using the draft alone loses unchanged target classes and widens held release.
    text = manifest_text().replace(
        "    count: {type: integer}", "    classes: {type: array, items: {type: string}}"
    )
    root, path = _items(tmp_path, text=text, values={"title": "One", "classes": ["restricted"]})
    held_id, _ = _held(
        path, 1, {"action": "update", "changes": {"title": "Changed"}}, target_key=KEY
    )
    with _capture(tmp_path, root, path) as (audit, captured), _connection() as conn:
        conn.execute("BEGIN")
        legacy_import.import_legacy_collection(conn, captured, audit=audit, context=CONTEXT)
        metadata = json.loads(
            conn.execute(
                "SELECT governance_json FROM held_candidates WHERE held_id=?", (held_id,)
            ).fetchone()[0]
        )
        assert metadata["classes"] == ["restricted"]


def test_incomplete_natural_keys_remain_null_not_invented_defaults(tmp_path):
    # Distinct rows with incomplete optional key fields must not acquire false conflicts.
    text = manifest_text().replace("natural_key: [title]", "natural_key: [title, count]")
    root, path = _items(tmp_path, text=text, values={"title": "One"})
    manifest = collections.load_manifest(root, path)
    (path.parent / "Items/Other.md").write_text(
        record_formats.render_markdown_item(
            manifest, {"title": "One"}, "22222222-2222-4222-8222-222222222222"
        )
    )
    with _capture(tmp_path, root, path) as (audit, captured), _connection() as conn:
        conn.execute("BEGIN")
        legacy_import.import_legacy_collection(conn, captured, audit=audit, context=CONTEXT)
        assert conn.execute("SELECT natural_key FROM items").fetchall() == [(None,), (None,)]
        assert all(
            json.loads(row[0]) == {"title": "One"}
            for row in conn.execute("SELECT values_json FROM items")
        )


def test_ordinary_canonical_items_may_use_held_namespace(tmp_path):
    # Holding restrictions must not reject a valid collection's ordinary item source.
    root, path = _items(
        tmp_path,
        text=manifest_text().replace("source: Items", "source: Held"),
        values={"title": "One"},
    )
    (path.parent / "Held/Original.md").rename(path.parent / "Held" / (KEY + ".md"))
    with _capture(tmp_path, root, path) as (audit, captured), _connection() as conn:
        assert len(captured.snapshot.records) == 1 and not captured.held
        conn.execute("BEGIN")
        legacy_import.import_legacy_collection(conn, captured, audit=audit, context=CONTEXT)
        assert conn.execute("SELECT item_key FROM items").fetchall() == [(KEY,)]
        assert conn.execute("SELECT COUNT(*) FROM held_candidates").fetchone()[0] == 0


@pytest.mark.parametrize("defect", ["unreadable", "unsafe", "overlap"])
def test_held_capture_never_silently_drops_unusable_input(tmp_path, defect):
    # Canonical held names must not disappear through parser or path failures.
    text = manifest_text().replace("source: Items", "source: Held") if defect == "overlap" else None
    root, path = _items(tmp_path, text=text)
    held_id, _ = _held(path, 1, {"action": "append", "item": {"title": "Held"}})
    target = path.parent / "Held" / (held_id + ".md")
    if defect == "unreadable":
        target.write_bytes(b"not a held candidate")
        expected = "HELD_NOT_FOUND"
    elif defect == "unsafe":
        target.unlink()
        target.symlink_to(path)
        expected = "UNSAFE_PATH"
    else:
        expected = "HELD_DIRECTORY_UNSAFE"
    with pytest.raises(
        (CollectionStoreError, collections.CollectionError, held_fs.HeldFsError), match=expected
    ):
        with _capture(tmp_path, root, path):
            pass
