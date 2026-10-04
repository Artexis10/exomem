"""Publication boundaries: committed reads, failed COMMIT, and displaced edits."""

import json
import os
import sqlite3
import threading

import pytest
from test_collection_store_writer import CID, KEY, create
from test_collection_store_writer import store as store

from exomem import get_page, held_fs
from exomem import structured_collections as collections
from exomem.collection_store import connection
from exomem.collection_store.writer import CollectionWriter


def update(store, receipt, title):
    return store.update_record(
        CID,
        item_key=KEY,
        changes={"title": title},
        why="correct",
        expected_container_hash=receipt["after_container_hash"],
        expected_item_version=receipt["after_item_hash"],
    )


def test_failed_commit_does_not_publish_proposed_view(store):
    # An SQLite COMMIT rejection must leave the acknowledged view and row intact.
    create(store)
    first = store.append_record(CID, item={"title": "One"}, item_key=KEY, why="observe")
    path = store.root / first["affected_paths"][0]
    before = path.read_bytes()
    store.connection.set_authorizer(
        lambda action, arg, *_: (
            sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_TRANSACTION and arg == "COMMIT"
            else sqlite3.SQLITE_OK
        )
    )
    try:
        with pytest.raises(sqlite3.DatabaseError):
            update(store, first, "Two")
    finally:
        store.connection.set_authorizer(None)
    assert path.read_bytes() == before
    assert store.connection.execute("SELECT row_version FROM items").fetchone() == (1,)
    assert not list(path.parent.glob(".exomem-collection-stage-*"))


def test_successive_installs_accept_previous_pending_view(store):
    # Lazy bookkeeping must not misclassify our previous successful install.
    create(store)
    result = store.append_record(CID, item={"title": "One"}, item_key=KEY, why="observe")
    for title in ("Two", "Three"):
        result = update(store, result, title)
        page = get_page.get_page(store.root, path=result["affected_paths"][0])
        assert page.frontmatter["title"] == title
    assert store.connection.execute("SELECT COUNT(*) FROM held_candidates").fetchone() == (0,)
    assert not list(store.root.rglob(".exomem-collection-aside-*"))


@pytest.mark.skipif(os.name == "nt", reason="POSIX no-clobber link fault probe")
def test_blocked_install_warns_without_changing_canonical_receipt(store, monkeypatch):
    # Filesystem failure after COMMIT is an advisory, with durable retry data.
    create(store)
    from exomem._held_fs_posix import PosixHeldFilesystem

    original = PosixHeldFilesystem.link

    def blocked(self, source, parent, leaf):
        if leaf.endswith(".md"):
            return held_fs.HeldResult(error=held_fs.HeldFsError("IO_REFUSED", "blocked"))
        return original(self, source, parent, leaf)

    monkeypatch.setattr(PosixHeldFilesystem, "link", blocked)
    receipt = store.append_record(
        CID, item={"title": "One"}, item_key=KEY, why="observe", request_id="blocked"
    )
    assert "projection_pending" in receipt["warnings"]
    canonical = json.loads(
        store.connection.execute(
            "SELECT receipt_json FROM txns WHERE request_id='blocked'"
        ).fetchone()[0]
    )
    assert "projection_pending" not in canonical.get("warnings", [])
    assert store.connection.execute("SELECT row_version FROM items").fetchone() == (1,)
    monkeypatch.setattr(PosixHeldFilesystem, "link", original)
    store.reconcile_views()
    assert (
        get_page.get_page(store.root, path=receipt["affected_paths"][0]).frontmatter["title"]
        == "One"
    )


def test_manifest_and_held_are_readable_before_acknowledgement(store):
    # A manifest and refused candidate are useful immediately to ordinary readers.
    receipt = create(store)
    manifest = get_page.get_page(store.root, path=receipt["affected_paths"][0])
    assert manifest.frontmatter["exomem_view"]["v"] == 1
    with pytest.raises(collections.CollectionError) as error:
        store.append_record(CID, item={"title": "One", "count": "bad"}, why="capture")
    held = get_page.get_page(store.root, path=error.value.details["held"]["path"])
    assert held.frontmatter["exomem_view"]["v"] == 1


def test_human_edit_survives_reopen_and_is_registered_on_reconcile(store):
    # Deferred bytes must remain owned by the committed descriptor across restart.
    create(store)
    first = store.append_record(CID, item={"title": "One"}, item_key=KEY, why="observe")
    path = store.root / first["affected_paths"][0]
    human = path.read_bytes().replace(b"title: One", b"title: Human")
    path.write_bytes(human)
    receipt = update(store, first, "Two")
    assert "projection_pending" in receipt["warnings"]
    assert store.connection.execute("SELECT COUNT(*) FROM held_candidates").fetchone() == (0,)
    root, db = store.root, store.handle.path
    store.handle.close()
    with connection.open_writer(db, lease_check=lambda: True) as handle:
        reopened = CollectionWriter(root, handle)
        reopened.reconcile_views()
        code, raw, diagnostics = reopened.connection.execute(
            "SELECT code,held_bytes,diagnostics_json FROM held_candidates WHERE kind='view-correction'"
        ).fetchone()
        assert code == "VIEW_CONFLICT" and raw == human
        assert json.loads(diagnostics)[0]["current_row_version"] == 2
        assert not list(root.rglob(".exomem-collection-aside-*"))
        assert (
            get_page.get_page(root, path=receipt["affected_paths"][0]).frontmatter["title"] == "Two"
        )


@pytest.mark.skipif(os.name == "nt", reason="POSIX no-clobber link race probe")
@pytest.mark.parametrize("collision", ["destination", "aside"])
def test_destination_created_in_install_gap_is_preserved(store, monkeypatch, collision):
    # Neither the public install target nor the private displacement target may clobber late bytes.
    create(store)
    first = store.append_record(CID, item={"title": "One"}, item_key=KEY, why="observe")
    path = store.root / first["affected_paths"][0]
    human = b"human bytes created in the gap\n"
    from exomem import _held_fs_posix as backend

    inserted = False

    def racing(original):
        def operation(source, leaf, **kwargs):
            nonlocal inserted
            selected = (
                leaf == path.name
                if collision == "destination"
                else leaf.startswith(".exomem-collection-aside-")
            )
            if selected and not inserted:
                inserted = True
                (path.parent / leaf).write_bytes(human)
            return original(source, leaf, **kwargs)

        return operation

    monkeypatch.setattr(backend, "_link", racing(backend._link))
    monkeypatch.setattr(backend, "_rename", racing(backend._rename))
    receipt = update(store, first, "Two")
    assert "projection_pending" in receipt["warnings"]
    assert any(p.read_bytes() == human for p in path.parent.glob(".exomem-collection-aside-*"))
    if collision == "aside":
        assert (
            get_page.get_page(store.root, path=first["affected_paths"][0]).frontmatter["title"]
            == "One"
        )
    store.reconcile_views()
    assert store.connection.execute("SELECT held_bytes FROM held_candidates").fetchall() == [
        (human,)
    ]
    assert (
        get_page.get_page(store.root, path=first["affected_paths"][0]).frontmatter["title"] == "Two"
    )


def test_reconcile_defers_valid_current_edit_but_holds_invalid_values(store):
    # Recovery cannot adopt an edit, and a valid stamp cannot conceal invalid data.
    create(store)
    receipt = store.append_record(
        CID, item={"title": "One", "count": 2}, item_key=KEY, why="observe"
    )
    path = store.root / receipt["affected_paths"][0]
    original = path.read_bytes()
    human = original.replace(b"title: One", b"title: Human")
    path.write_bytes(human)
    result = store.reconcile_views()
    assert "projection_pending" in result["warnings"] and path.read_bytes() == human
    assert store.connection.execute("SELECT COUNT(*) FROM held_candidates").fetchone() == (0,)
    invalid = original.replace(b"count: 2", b"count: broken")
    path.write_bytes(invalid)
    store.reconcile_views()
    assert store.connection.execute("SELECT code,held_bytes FROM held_candidates").fetchone() == (
        "VIEW_INVALID",
        invalid,
    )


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("i", "00000000-0000-4000-8000-000000000001", "VIEW_FOREIGN"),
        ("h", "0" * 12, "VIEW_INVALID"),
        ("v", "broken", "VIEW_INVALID"),
    ],
)
def test_reconcile_stamp_precedence(store, field, value, code):
    # Foreign identity, tampered digest, and malformed version have distinct outcomes.
    from exomem import vault

    create(store)
    receipt = store.append_record(CID, item={"title": "One"}, item_key=KEY, why="observe")
    path = store.root / receipt["affected_paths"][0]
    fm, body, _ = vault.parse_frontmatter(path.read_text(), strict=True)
    fm["exomem_view"][field] = value
    raw = ("---\n" + vault.serialize_frontmatter(fm) + "\n---\n" + body).encode()
    path.write_bytes(raw)
    store.reconcile_views()
    assert store.connection.execute("SELECT code,held_bytes FROM held_candidates").fetchone() == (
        code,
        raw,
    )
    if code == "VIEW_FOREIGN":
        with pytest.raises(connection.CollectionStoreError, match="COLLECTION_STORE_DIVERGED"):
            store.append_record(CID, item={"title": "Two"}, why="observe")


def test_held_retirement_removes_the_view_after_commit(store):
    # Discard must remove the rendered refusal as well as the canonical candidate.
    create(store)
    with pytest.raises(collections.CollectionError) as error:
        store.append_record(CID, item={"title": "One", "count": "bad"}, why="capture")
    held = error.value.details["held"]
    path = store.root / held["path"]
    assert path.exists()
    store.discard_held(CID, held=held["held_id"], why="discard")
    assert not path.exists()


def test_publication_scratch_is_private_on_egress(store):
    # A preserved human aside must not be exposed as a regular vault page.
    from exomem import reserved_paths
    from exomem.list_directory import list_directory

    create(store)
    first = store.append_record(CID, item={"title": "One"}, item_key=KEY, why="observe")
    path = store.root / first["affected_paths"][0]
    path.write_bytes(path.read_bytes().replace(b"title: One", b"title: Private"))
    update(store, first, "Two")
    aside = next(path.parent.glob(".exomem-collection-aside-*"))
    rel = aside.relative_to(store.root).as_posix()
    assert reserved_paths.classify_logical(rel).descriptor_id == "collection-publication"
    with pytest.raises(get_page.GetError):
        get_page.get_page(store.root, path=rel)
    listing = list_directory(store.root, path=path.parent.relative_to(store.root).as_posix())
    assert aside.name not in str(listing)


@pytest.mark.parametrize("interruption", ["before_install", "stage_unlink"])
def test_restart_recovers_committed_stage_and_removes_only_orphan_staging(
    store, monkeypatch, interruption
):
    # Both a pre-install crash and an interrupted own link pair recover without deleting unknown asides.
    from exomem.collection_store import views

    create(store)
    if interruption == "before_install":
        original = views.PublicationBatch.publish
        monkeypatch.setattr(
            views.PublicationBatch,
            "publish",
            lambda self: (_ for _ in ()).throw(SystemExit("crash")),
        )
        with pytest.raises(SystemExit, match="crash"):
            store.append_record(CID, item={"title": "One"}, item_key=KEY, why="observe")
        monkeypatch.setattr(views.PublicationBatch, "publish", original)
    else:
        if os.name == "nt":
            pytest.skip("POSIX interrupted stage unlink probe")
        from exomem._held_fs_posix import PosixHeldFilesystem

        original = PosixHeldFilesystem.unlink

        def blocked(self, file):
            if file.name.startswith(views.STAGE_PREFIX):
                return held_fs.HeldResult(error=held_fs.HeldFsError("IO_REFUSED", "blocked"))
            return original(self, file)

        monkeypatch.setattr(PosixHeldFilesystem, "unlink", blocked)
        receipt = store.append_record(CID, item={"title": "One"}, item_key=KEY, why="observe")
        assert "projection_pending" in receipt["warnings"]
        monkeypatch.setattr(PosixHeldFilesystem, "unlink", original)
    path = store.connection.execute("SELECT view_path FROM items").fetchone()[0]
    parent = (store.root / path).parent
    orphan = parent / (views.STAGE_PREFIX + "f" * 32)
    unknown = parent / (views.ASIDE_PREFIX + "f" * 32 + "-0")
    orphan.write_bytes(b"rolled-back staged render")
    unknown.write_bytes(b"unknown preserved human edit")
    root, db = store.root, store.handle.path
    store.handle.close()
    with connection.open_writer(db, lease_check=lambda: True) as handle:
        reopened = CollectionWriter(root, handle)
        result = reopened.reconcile_views()
        assert get_page.get_page(root, path=path).frontmatter["title"] == "One"
        assert not orphan.exists()
        assert unknown.read_bytes() == b"unknown preserved human edit"
        assert "projection_pending" in result["warnings"]
        if interruption == "stage_unlink":
            assert result["recovered_stages"][0]["provenance"] == "committed_baseline"
            second = reopened.update_record(
                CID,
                item_key=KEY,
                changes={"title": "Two"},
                why="correct",
                expected_container_hash=receipt["after_container_hash"],
                expected_item_version=receipt["after_item_hash"],
            )
            assert (
                get_page.get_page(root, path=second["affected_paths"][0]).frontmatter["title"]
                == "Two"
            )


@pytest.mark.skipif(os.name == "nt", reason="POSIX no-clobber link race probe")
def test_repeated_install_contention_preserves_every_displaced_sequence(store, monkeypatch):
    # A second racing destination ends the retry instead of looping against an editor.
    create(store)
    first = store.append_record(CID, item={"title": "One"}, item_key=KEY, why="observe")
    path = store.root / first["affected_paths"][0]
    from exomem._held_fs_posix import PosixHeldFilesystem

    original = PosixHeldFilesystem.link
    sequences = [b"first late edit\n", b"second late edit\n"]
    attempts = 0

    def racing(self, source, parent, leaf):
        nonlocal attempts
        if leaf == path.name:
            path.write_bytes(sequences[attempts])
            attempts += 1
        return original(self, source, parent, leaf)

    monkeypatch.setattr(PosixHeldFilesystem, "link", racing)
    receipt = update(store, first, "Two")
    assert attempts == 2 and path.read_bytes() == sequences[-1]
    assert "projection_pending" in receipt["warnings"]
    monkeypatch.setattr(PosixHeldFilesystem, "link", original)
    store.reconcile_views()
    assert {
        raw for (raw,) in store.connection.execute("SELECT held_bytes FROM held_candidates")
    } == set(sequences)


def test_reconcile_continuation_visits_views_beyond_the_first_window(store):
    # A bounded recovery window must not starve later paths behind quiet first rows.
    create(store)
    store.append_record(CID, item={"title": "One"}, item_key=KEY, why="observe")
    first = store.reconcile_views(limit=1)
    second = store.reconcile_views(limit=1, after=first["next_path"])
    assert first["examined"] == second["examined"] == 1
    assert first["next_path"] is not None and second["next_path"] is None


def test_displaced_hard_link_alias_is_private_and_cannot_fail_a_commit(store):
    # Moving a human inode aside must also reserve an already-existing hard-link alias.
    create(store)
    first = store.append_record(CID, item={"title": "One"}, item_key=KEY, why="observe")
    path = store.root / first["affected_paths"][0]
    path.write_bytes(path.read_bytes().replace(b"title: One", b"title: Human"))
    assert (
        get_page.get_page(store.root, path=first["affected_paths"][0]).frontmatter["title"]
        == "Human"
    )
    alias = path.parent / "alias.md"
    os.link(path, alias)
    relative = alias.relative_to(store.root).as_posix()
    receipt = update(store, first, "Two")
    assert "projection_pending" in receipt["warnings"]
    assert any(
        b"title: Human" in aside.read_bytes()
        for aside in path.parent.glob(".exomem-collection-aside-*")
    )
    with pytest.raises(get_page.GetError):
        get_page.get_page(store.root, path=relative)
    store.reconcile_views()
    assert b"title: Human" in alias.read_bytes()
    assert any(
        b"title: Human" in aside.read_bytes()
        for aside in path.parent.glob(".exomem-collection-aside-*")
    )
    with pytest.raises(get_page.GetError):
        get_page.get_page(store.root, path=relative)


def test_retired_held_displacement_retains_restricted_candidate_policy(store):
    # A private candidate's edited view must not become public after its original is discarded.
    from test_collection_store_writer import manifest_path, manifest_text
    from test_governance_egress import _external, write_rule, write_scope

    from exomem.collection_store.preview import preview_store
    from exomem.governance.principal import owner_principal, request_scope
    from exomem.record_memory import record_memory

    text = manifest_text().replace(
        "    count:", "    tags: {type: array, items: {type: string}}\n    count:"
    )
    store.create_collection(manifest_path(), text, why="create")
    with pytest.raises(collections.CollectionError) as error:
        store.append_record(
            CID, item={"title": "Private", "tags": ["secret"], "count": "bad"}, why="capture"
        )
    held = error.value.details["held"]
    path = store.root / held["path"]
    human = path.read_bytes() + b"\nHuman correction\n"
    path.write_bytes(human)
    write_scope(store.root, paths="Unrelated/**")
    scope = store.root / "Knowledge Base/_Governance/scopes/patterns.yaml"
    scope.write_text(scope.read_text() + "tags: [secret]\n")
    write_rule(store.root, ceiling=0)
    with request_scope(owner_principal()):
        store.discard_held(CID, held=held["held_id"], why="discard")
        store.reconcile_views()
    raw, metadata, correction_path = store.connection.execute(
        "SELECT held_bytes,governance_json,view_path FROM held_candidates"
    ).fetchone()
    assert raw == human and json.loads(metadata)["tags"] == ["secret"]
    page = get_page.get_page(store.root, path=correction_path)
    assert page.frontmatter["tags"] == ["secret"]
    with preview_store(store.root, store.handle), request_scope(_external()):
        result = record_memory(store.root, "inspect", collection=CID)
        assert result["coverage"]["held"] == 0 and "Human correction" not in str(result)


def test_manifest_revision_preserves_restricted_view_correction_policy(store):
    # Revising a title must not turn a restricted displaced edit into a public Held candidate.
    from test_collection_store_writer import manifest_path, manifest_text
    from test_governance_egress import _external, write_rule, write_scope

    from exomem.collection_store.preview import preview_store
    from exomem.governance.principal import owner_principal, request_scope
    from exomem.record_memory import record_memory

    text = manifest_text().replace("title: Work", "title: Work\nprojects: [Alpha]").replace(
        "    count:",
        "    tags: {type: array, items: {type: string}}\n"
        "    classes: {type: array, items: {type: string}}\n    count:",
    )
    store.create_collection(manifest_path(), text, why="create")
    first = store.append_record(
        CID, item={"title": "One", "tags": ["secret"], "classes": ["restricted"]},
        item_key=KEY, why="capture",
    )
    path = store.root / first["affected_paths"][0]
    human = path.read_bytes().replace(b"title: One", b"title: Human")
    path.write_bytes(human)
    update(store, first, "Two")
    store.reconcile_views()
    held_id, raw = store.connection.execute(
        "SELECT held_id,held_bytes FROM held_candidates WHERE kind='view-correction'"
    ).fetchone()
    assert raw == human
    write_scope(store.root, paths="Unrelated/**")
    scope = store.root / "Knowledge Base/_Governance/scopes/patterns.yaml"
    scope.write_text(scope.read_text() + "tags: [secret]\n")
    write_rule(store.root, ceiling=0)

    with preview_store(store.root, store.handle):
        with request_scope(_external()):
            assert record_memory(store.root, "inspect", collection=CID)["coverage"]["held"] == 0
        # A project revision also changes the Held envelope, unlike the title-only revision.
        for revised in (
            text.replace("title: Work", "title: Revised"),
            text.replace("title: Work", "title: Revised").replace("[Alpha]", "[Beta]"),
        ):
            with request_scope(owner_principal()):
                guards = record_memory(store.root, "inspect", collection=CID)["lifecycle_guards"]
                record_memory(
                    store.root, "revise", collection=CID, manifest_text=revised,
                    why="revise", **guards,
                )
            with request_scope(_external()):
                result = record_memory(store.root, "inspect", collection=CID)
                assert result["coverage"]["held"] == 0
                assert held_id not in str(result) and "Human" not in str(result)
            with request_scope(owner_principal()):
                preserved, metadata, held_path = store.connection.execute(
                    "SELECT held_bytes,governance_json,view_path FROM held_candidates WHERE held_id=?",
                    (held_id,),
                ).fetchone()
                assert preserved == human
                assert json.loads(metadata) == {
                    "projects": ["beta" if "[Beta]" in revised else "alpha"],
                    "tags": ["secret"], "classes": ["restricted"],
                }
                assert get_page.get_page(store.root, path=held_path).frontmatter["projects"] == (
                    json.loads(metadata)["projects"]
                )


def test_failed_rehold_cannot_downgrade_displaced_held_policy(store):
    # A less restricted retry must not expose edits to its previous secret Held envelope.
    from test_collection_store_writer import manifest_path, manifest_text
    from test_governance_egress import _external, write_rule, write_scope

    from exomem.cli_ops import OpError
    from exomem.collection_store.preview import preview_store
    from exomem.governance.principal import owner_principal, request_scope
    from exomem.record_memory import record_memory

    text = manifest_text().replace(
        "    count:", "    tags: {type: array, items: {type: string}}\n    count:"
    )
    store.create_collection(manifest_path(), text, why="create")
    with pytest.raises(collections.CollectionError) as original:
        store.append_record(CID, item={"title": "Private", "tags": ["secret"], "count": "bad"}, why="capture")
    held = original.value.details["held"]
    path = store.root / held["path"]
    human = path.read_bytes() + b"\nPrivate human correction\n"
    path.write_bytes(human)
    write_scope(store.root, paths="Unrelated/**")
    scope = store.root / "Knowledge Base/_Governance/scopes/patterns.yaml"
    scope.write_text(scope.read_text() + "tags: [secret]\n")
    write_rule(store.root, ceiling=0)
    with preview_store(store.root, store.handle):
        with request_scope(owner_principal()):
            with pytest.raises(OpError, match="SCHEMA_FIELD_TYPE"):
                record_memory(store.root, "append", collection=CID, held=held["held_id"],
                              item={"tags": [], "count": "still bad"}, why="correct")
            store.reconcile_views()
        with request_scope(_external()):
            result = record_memory(store.root, "inspect", collection=CID)
            assert result["coverage"]["held"] == 1
            assert "Private human correction" not in str(result)
        correction_id, raw, metadata = store.connection.execute(
            "SELECT held_id,held_bytes,governance_json FROM held_candidates WHERE kind='view-correction'"
        ).fetchone()
        assert raw == human and json.loads(metadata)["tags"] == ["secret"]
        assert correction_id not in str(result)


@pytest.mark.parametrize("kind", ["item", "manifest"])
def test_displaced_render_keeps_policy_before_item_or_manifest_revision(store, kind):
    # These sources store policy in different canonical tables; neither may use the replacement's policy.
    from test_collection_store_writer import manifest_path, manifest_text
    from test_governance_egress import _external, write_rule, write_scope

    from exomem.collection_store.preview import preview_store
    from exomem.governance.principal import owner_principal, request_scope
    from exomem.record_memory import record_memory

    text = manifest_text().replace(
        "    count:", "    tags: {type: array, items: {type: string}}\n    count:"
    )
    if kind == "manifest":
        text = text.replace("title: Work", "title: Work\ntags: [secret]")
    store.create_collection(manifest_path(), text, why="create")
    first = store.append_record(CID, item={"title": "One", "tags": ["secret"]}, item_key=KEY, why="observe") if kind == "item" else None
    path = store.root / (first["affected_paths"][0] if first else manifest_path())
    human = path.read_bytes().replace(b"title: One" if first else b"title: Work", b"title: Private edit")
    path.write_bytes(human)
    write_scope(store.root, paths="Unrelated/**")
    scope = store.root / "Knowledge Base/_Governance/scopes/patterns.yaml"
    scope.write_text(scope.read_text() + "tags: [secret]\n")
    write_rule(store.root, ceiling=0)
    with preview_store(store.root, store.handle):
        with request_scope(owner_principal()):
            if first:
                record_memory(store.root, "update", collection=CID, item_key=KEY, changes={"tags": []},
                              expected_container_hash=first["after_container_hash"], expected_item_version=first["after_item_hash"], why="correct")
            else:
                guards = record_memory(store.root, "inspect", collection=CID)["lifecycle_guards"]
                record_memory(store.root, "revise", collection=CID,
                              manifest_text=text.replace("tags: [secret]", "tags: []"), why="revise", **guards)
            store.reconcile_views()
        with request_scope(_external()):
            result = record_memory(store.root, "inspect", collection=CID)
            assert result["coverage"]["held"] == 0
        held_id, raw, metadata = store.connection.execute(
            "SELECT held_id,held_bytes,governance_json FROM held_candidates WHERE kind='view-correction'"
        ).fetchone()
        assert raw == human and json.loads(metadata)["tags"] == ["secret"]
        assert held_id not in str(result)


@pytest.mark.skipif(os.name == "nt", reason="POSIX late foreign input probe")
def test_foreign_preflight_commits_recovery_without_requested_update(store, monkeypatch):
    # A foreign view arriving during publication cannot ride the next business transaction.
    from exomem import vault
    from exomem._held_fs_posix import PosixHeldFilesystem
    from exomem.collection_store.preview import preview_store
    from exomem.governance.principal import owner_principal, request_scope
    from exomem.record_memory import record_memory

    create(store)
    first = store.append_record(CID, item={"title": "One"}, item_key=KEY, why="observe")
    path = store.root / first["affected_paths"][0]
    fm, body, _ = vault.parse_frontmatter(path.read_text(), strict=True)
    fm["exomem_view"]["i"] = "00000000-0000-4000-8000-000000000001"
    foreign = ("---\n" + vault.serialize_frontmatter(fm) + "\n---\n" + body).encode()
    link = PosixHeldFilesystem.link
    injected = False

    def late_foreign(self, source, parent, leaf):
        nonlocal injected
        if leaf == path.name and not injected:
            path.write_bytes(foreign)
            injected = True
        return link(self, source, parent, leaf)

    monkeypatch.setattr(PosixHeldFilesystem, "link", late_foreign)
    second = update(store, first, "Two")
    monkeypatch.setattr(PosixHeldFilesystem, "link", link)
    assert "projection_pending" in second["warnings"]
    before = tuple(store.connection.execute("SELECT generation,audit_head FROM collections").fetchone())
    audit_count = store.connection.execute("SELECT COUNT(*) FROM audit_effects").fetchone()[0]
    receipts = store.connection.execute("SELECT request_id,request_hash,receipt_json FROM txns ORDER BY txn_id").fetchall()
    with preview_store(store.root, store.handle), request_scope(owner_principal()):
        with pytest.raises(connection.CollectionStoreError, match="COLLECTION_STORE_DIVERGED"):
            record_memory(store.root, "update", collection=CID, item_key=KEY, changes={"title": "Three"},
                          expected_container_hash=second["after_container_hash"],
                          expected_item_version=second["after_item_hash"], why="correct")
    assert store.connection.execute("SELECT row_version,values_json FROM items").fetchone() == (2, '{"title":"Two"}')
    assert tuple(store.connection.execute("SELECT generation,audit_head FROM collections").fetchone()) == before
    assert store.connection.execute("SELECT COUNT(*) FROM audit_effects").fetchone()[0] == audit_count
    assert store.connection.execute("SELECT request_id,request_hash,receipt_json FROM txns ORDER BY txn_id").fetchall() == receipts
    db = store.handle.path
    store.handle.close()
    with connection.open_writer(db, lease_check=lambda: True) as handle:
        reopened = CollectionWriter(store.root, handle)
        reopened.reconcile_views()
        assert handle.connection.execute("SELECT value FROM store_meta WHERE key='diverged'").fetchone() == ("1",)
        assert handle.connection.execute("SELECT code,held_bytes FROM held_candidates").fetchone() == ("VIEW_FOREIGN", foreign)


@pytest.mark.skipif(os.name == "nt", reason="POSIX staging hard-link race probe")
def test_public_alias_read_waits_for_staging_identity_publication(store, monkeypatch):
    # A generic reader cannot enter between scratch creation and phase identity publication.
    create(store)
    db = store.handle.path
    store.handle.close()
    entered, release, reading, finished = (threading.Event() for _ in range(4))
    outcomes = []
    stage_path = []
    from exomem._held_fs_posix import PosixHeldFilesystem

    original = PosixHeldFilesystem.write

    def paused(self, file, data):
        if file.name.startswith(".exomem-collection-stage-"):
            stage_path.append(store.root / "Knowledge Base/Records/Work/Items" / file.name)
            entered.set()
            assert release.wait(10)
        return original(self, file, data)

    def mutate():
        try:
            with connection.open_writer(db, lease_check=lambda: True) as handle:
                outcomes.append(
                    CollectionWriter(store.root, handle).append_record(
                        CID, item={"title": "Private stage"}, item_key=KEY, why="capture"
                    )
                )
        except BaseException as error:  # noqa: BLE001 - thread outcome assertion
            outcomes.append(error)

    def read():
        reading.set()
        try:
            outcomes.append(get_page.get_page(store.root, path="Knowledge Base/alias.md"))
        except get_page.GetError as error:
            outcomes.append(error)
        finally:
            finished.set()

    monkeypatch.setattr(PosixHeldFilesystem, "write", paused)
    writer = threading.Thread(target=mutate)
    reader = threading.Thread(target=read)
    writer.start()
    try:
        assert entered.wait(10), outcomes
        os.link(stage_path[0], store.root / "Knowledge Base/alias.md")
        reader.start()
        assert reading.wait(10) and not finished.wait(0.1)
    finally:
        release.set()
        writer.join(10)
        if reader.ident is not None:
            reader.join(10)
    assert not writer.is_alive() and not reader.is_alive()
    assert any(isinstance(result, get_page.GetError) for result in outcomes)
    assert any(
        isinstance(result, dict) and "projection_pending" in result["warnings"]
        for result in outcomes
    )
