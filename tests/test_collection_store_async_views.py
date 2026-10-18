"""Committed records stay current while derived files wait for publication."""

import json
import sqlite3

import pytest
from test_collection_store_writer import CID, KEY, manifest_path, manifest_text
from test_collection_store_writer import store as store
from test_governance_egress import _external, write_rule, write_scope

from exomem import get_page, held_fs, vault
from exomem.collection_store import capability, connection
from exomem.collection_store.preview import preview_store
from exomem.collection_store.writer import CollectionWriter
from exomem.governance.principal import request_scope
from exomem.record_memory import record_memory


def test_sqlite_save_precedes_item_and_manifest_publication(store):
    """A file failure cannot delay save, and a missing view cannot hide a committed record."""
    store.create_collection(manifest_path(), manifest_text(), why="create", scaffold=False)
    receipt = store.append_record(CID, item={"title": "Committed"}, item_key=KEY, why="capture")
    relative = receipt["affected_paths"][0]
    assert not (store.root / relative).exists()
    assert not (store.root / manifest_path()).exists()
    pending = store.connection.execute(
        "SELECT kind,pending_sha256,install_json FROM projection_state WHERE kind IN ('item','manifest')"
    ).fetchall()
    assert len(pending) == 2
    assert all(digest and json.loads(descriptor)["render_context"]["sha256"] == digest
               for _, digest, descriptor in pending)
    with preview_store(store.root, store.handle):
        query = record_memory(store.root, "query", collection=CID)
        inspection = record_memory(store.root, "inspect", collection=CID)
    assert query["rows"][0]["title"] == "Committed"
    assert inspection["projection"]["pending_views"] >= 2
    store.reconcile_views()
    assert get_page.get_page(store.root, path=relative).frontmatter["title"] == "Committed"
    assert get_page.get_page(store.root, path=manifest_path()).frontmatter["exomem_view"]["v"] == 1


def test_queued_versions_keep_installed_predecessor_policy_after_restart(store, monkeypatch):
    """A v2 save made after v3 and v4 queue must retain v2 policy when held after restart."""
    monkeypatch.setattr(capability, "RELEASED", capability.RELEASED | {capability.COLLECTION_VIEWS_V1})
    text = manifest_text().replace(
        "    count: {type: integer}\n",
        "    count: {type: integer}\n    tags: {type: array, items: {type: string}}\n",
    )
    store.create_collection(manifest_path(), text, why="create", scaffold=False)
    receipt = store.append_record(CID, item={"title": "One"}, item_key=KEY, why="capture")
    store.reconcile_views()
    receipt = store.update_record(CID, item_key=KEY, changes={"title": "Two", "tags": ["sealed"]},
        why="correct", expected_container_hash=receipt["after_container_hash"],
        expected_item_version=receipt["after_item_hash"])
    store.reconcile_views()  # v2 is installed; promotion happens on a later pass.
    relative = receipt["affected_paths"][0]
    path = store.root / relative
    physical = path.read_bytes()
    assert b"title: Two" in physical
    for title in ("Three", "Four"):
        receipt = store.update_record(CID, item_key=KEY, changes={"title": title, "tags": []},
            why="correct", expected_container_hash=receipt["after_container_hash"],
            expected_item_version=receipt["after_item_hash"])
    assert path.read_bytes() == physical
    root, db = store.root, store.handle.path
    store.handle.close()
    write_scope(root, paths="Unrelated/**")
    scope = root / "Knowledge Base/_Governance/scopes/patterns.yaml"
    scope.write_text(scope.read_text() + "tags: [sealed]\n")
    write_rule(root, ceiling=0)
    human = physical.replace(b"title: Two", b"title: Human")
    path.write_bytes(human)
    with connection.open_writer(db, lease_check=lambda: True) as handle:
        from test_collection_store_edit_back import SETTLED, passes

        reopened = CollectionWriter(root, handle)
        reopened.reconcile_views()
        passes(reopened, 0.0, SETTLED)
        code, raw, diagnostics = reopened.connection.execute(
            "SELECT code,held_bytes,diagnostics_json FROM held_candidates WHERE kind='view-correction'"
        ).fetchone()
        assert code == "VIEW_CONFLICT" and raw == human
        assert json.loads(diagnostics)[0]["base_row_version"] == 2
        assert reopened.connection.execute("SELECT row_version FROM items").fetchone() == (4,)
        with preview_store(root, handle), request_scope(_external()):
            query = record_memory(root, "query", collection=CID)
            inspection = record_memory(root, "inspect", collection=CID)
        assert query["rows"][0]["title"] == "Four"
        assert inspection["projection"]["held_view_corrections"] == 0
        assert "Human" not in str(inspection)


def test_edited_unpromoted_input_keeps_old_policy_despite_a_forged_current_stamp(store):
    """A never-installed public queue must not authorize private bytes from the physical predecessor."""
    text = manifest_text().replace("    count:", "    tags: {type: array, items: {type: string}}\n    count:")
    store.create_collection(manifest_path(), text, why="create", scaffold=False)
    first = store.append_record(CID, item={"title": "Private", "tags": ["sealed"]}, item_key=KEY, why="capture")
    store.reconcile_views()
    relative = first["affected_paths"][0]
    path = store.root / relative
    private = path.read_bytes()
    path.write_bytes(private.replace(b"title: Private", b"title: Unseen private save"))
    latest = store.update_record(CID, item_key=KEY, changes={"title": "Public", "tags": []}, why="publish",
        expected_container_hash=first["after_container_hash"], expected_item_version=first["after_item_hash"])
    root, db = store.root, store.handle.path
    store.handle.close()
    write_scope(root, paths="Unrelated/**")
    scope = root / "Knowledge Base/_Governance/scopes/patterns.yaml"
    scope.write_text(scope.read_text() + "tags: [sealed]\n")
    write_rule(root, ceiling=0)
    with connection.open_writer(db, lease_check=lambda: True) as handle:
        reopened = CollectionWriter(root, handle)
        frontmatter, body, _ = vault.parse_frontmatter(path.read_text(), strict=True)
        frontmatter.update(title="Private text with a forged public stamp", tags=[], count="invalid")
        frontmatter["exomem_view"]["v"] = 2
        frontmatter["exomem_view"]["h"] = handle.connection.execute("SELECT payload_hash FROM items").fetchone()[0][:12]
        forged = ("---\n" + vault.serialize_frontmatter(frontmatter) + "\n---\n" + body).encode()
        path.write_bytes(forged)
        reopened.reconcile_views()
        raw, metadata, quarantined = handle.connection.execute(
            "SELECT held_bytes,governance_json,quarantined FROM held_candidates WHERE kind='view-correction'"
        ).fetchone()
        assert raw == forged and json.loads(metadata)["tags"] == ["sealed"] and quarantined == 0
        assert reopened.current_guards(CID, KEY)[1] == latest["after_item_hash"]
        with preview_store(root, handle), request_scope(_external()):
            query = record_memory(root, "query", collection=CID)
            inspection = record_memory(root, "inspect", collection=CID)
        assert query["rows"][0]["title"] == "Public"
        assert inspection["projection"]["held_view_corrections"] == 0
        assert "Private text" not in str(inspection)


@pytest.mark.parametrize("failure", ["commit", "install"])
def test_installation_readiness_is_committed_before_install_and_rolls_back_with_stage(store, monkeypatch, failure):
    """Publication must not use uncommitted readiness or retain readiness from a refused projector commit."""
    from exomem.collection_store import views

    store.create_collection(manifest_path(), manifest_text(), why="create", scaffold=False)
    receipt = store.append_record(CID, item={"title": "Committed"}, item_key=KEY, why="capture")
    relative = receipt["affected_paths"][0]
    if failure == "commit":
        store.connection.set_authorizer(lambda action, arg, *_: sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_TRANSACTION and arg == "COMMIT" else sqlite3.SQLITE_OK)
        try:
            with pytest.raises(sqlite3.DatabaseError):
                store.reconcile_views()
        finally:
            store.connection.set_authorizer(None)
        descriptor = json.loads(store.connection.execute(
            "SELECT install_json FROM projection_state WHERE kind='item'").fetchone()[0])
        assert descriptor.get("install_ready") is None
        assert not list(store.root.rglob(".exomem-collection-stage-*"))
    else:
        original = views.PublicationBatch._install
        observed = []

        def interrupted(batch, publication):
            with sqlite3.connect(f"file:{store.handle.path}?mode=ro", uri=True) as reader:
                descriptor, version, digest = reader.execute(
                    "SELECT install_json,pending_row_version,pending_sha256 FROM projection_state WHERE path=?",
                    (publication.path,),
                ).fetchone()
            assert not store.connection.in_transaction
            assert json.loads(descriptor)["install_ready"] == [version, digest]
            observed.append(publication.path)
            raise held_fs.HeldFsError("IO_REFUSED", "interrupted after commit")

        monkeypatch.setattr(views.PublicationBatch, "_install", interrupted)
        assert "projection_pending" in store.reconcile_views()["warnings"]
        assert relative in observed
        monkeypatch.setattr(views.PublicationBatch, "_install", original)
    assert not (store.root / relative).exists()
    assert store.connection.execute("SELECT row_version FROM items").fetchone() == (1,)
    store.reconcile_views()
    assert get_page.get_page(store.root, path=relative).frontmatter["title"] == "Committed"


@pytest.mark.parametrize("source", ["aside", "ambiguous_target"])
def test_recovery_keeps_aside_authority_separate_from_a_ready_public_render(store, monkeypatch, source):
    """A public installation cannot release a private aside or an input with ambiguous physical ancestry."""
    from exomem.collection_store import views

    text = manifest_text().replace("    count:", "    tags: {type: array, items: {type: string}}\n    count:")
    store.create_collection(manifest_path(), text, why="create", scaffold=False)
    first = store.append_record(CID, item={"title": "Private", "tags": ["sealed"]}, item_key=KEY, why="capture")
    store.reconcile_views()
    relative = first["affected_paths"][0]
    path = store.root / relative
    human = path.read_bytes().replace(b"title: Private", b"title: Private human save")
    store.update_record(CID, item_key=KEY, changes={"title": "Public", "tags": []}, why="publish",
        expected_container_hash=first["after_container_hash"], expected_item_version=first["after_item_hash"])
    with monkeypatch.context() as interrupted:
        if source == "aside":
            original = views.PublicationBatch._asides

            def changed_aside(batch, publication):
                if publication.path == relative:
                    aside = path.parent / (views.ASIDE_PREFIX + publication.descriptor["token"] + "-0")
                    aside.write_bytes(human)
                return original(batch, publication)

            interrupted.setattr(views.PublicationBatch, "_asides", changed_aside)
        else:
            original = views.PublicationBatch._install

            def before_install(batch, publication):
                if publication.path == relative:
                    raise held_fs.HeldFsError("IO_REFUSED", "interrupted installation")
                return original(batch, publication)

            interrupted.setattr(views.PublicationBatch, "_install", before_install)
        assert "projection_pending" in store.reconcile_views()["warnings"]
    if source == "ambiguous_target":
        path.write_bytes(human)
    root, db = store.root, store.handle.path
    store.handle.close()
    write_scope(root, paths="Unrelated/**")
    scope = root / "Knowledge Base/_Governance/scopes/patterns.yaml"
    scope.write_text(scope.read_text() + "tags: [sealed]\n")
    write_rule(root, ceiling=0)
    with connection.open_writer(db, lease_check=lambda: True) as handle:
        reopened = CollectionWriter(root, handle)
        reopened.reconcile_views()
        raw, metadata, quarantined = handle.connection.execute(
            "SELECT held_bytes,governance_json,quarantined FROM held_candidates WHERE kind='view-correction'"
        ).fetchone()
        assert raw == human
        assert quarantined == (source == "ambiguous_target")
        if source == "aside":
            assert json.loads(metadata)["tags"] == ["sealed"]
        with preview_store(root, handle), request_scope(_external()):
            inspection = record_memory(root, "inspect", collection=CID)
            query = record_memory(root, "query", collection=CID)
        assert inspection["projection"]["held_view_corrections"] == 0
        assert query["rows"][0]["title"] == "Public"


def test_a_changed_physical_predecessor_invalidates_its_precommit_policy_proof(store, monkeypatch):
    """A guarded observation cannot authorize a later save after the editor replaces those bytes."""
    store.create_collection(manifest_path(), manifest_text(), why="create", scaffold=False)
    first = store.append_record(CID, item={"title": "One"}, item_key=KEY, why="capture")
    store.reconcile_views()
    path = store.root / first["affected_paths"][0]
    human = path.read_bytes().replace(b"title: One", b"title: Concurrent human save")
    original = CollectionWriter._precommit

    def editing(writer, manifest):
        path.write_bytes(human)
        return original(writer, manifest)

    monkeypatch.setattr(CollectionWriter, "_precommit", editing)
    with pytest.raises(vault.PathGuardError):
        store.update_record(CID, item_key=KEY, changes={"title": "Two"}, why="correct",
            expected_container_hash=first["after_container_hash"], expected_item_version=first["after_item_hash"])
    assert path.read_bytes() == human
    assert store.connection.execute("SELECT row_version FROM items").fetchone() == (1,)
