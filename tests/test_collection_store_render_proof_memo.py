"""A proof that view bytes are the current render is reused only while the store is unchanged.

Each case warms the proof first and checks that the next decision reused it, so a memo that never
hits cannot pass. Only the bytes, the store and the tombstones change between decisions.
"""

import sqlite3

import pytest
from test_collection_store_writer import CID, KEY, manifest_path, manifest_text
from test_collection_store_writer import store as store
from test_governance_egress import _external, write_rule, write_scope

from exomem.collection_store import views
from exomem.collection_store.preview import preview_store
from exomem.governance import egress, lifecycle
from exomem.governance.principal import owner_principal, request_scope


@pytest.fixture
def renders(monkeypatch):
    """The paths whose current render an authorization decision built, in order."""
    built = []
    render = views.render_view

    def counted(conn, identity, projection, *args, **kwargs):
        if "path" in projection:  # a decision renders a stored row; the writer renders a bare reference
            built.append(projection["path"])
        return render(conn, identity, projection, *args, **kwargs)

    monkeypatch.setattr(views, "render_view", counted)
    return built


def _published_page(store, title="Current"):
    store.create_collection(manifest_path(), manifest_text(), why="create")
    receipt = store.append_record(CID, item={"title": title}, item_key=KEY, why="capture")
    store.reconcile_views()
    path = receipt["affected_paths"][0]
    return path, {"items": [{"path": path}]}, receipt


def test_edited_view_bytes_are_not_covered_by_the_proof_of_the_bytes_they_replaced(store, renders):
    """Editing a view file leaves the store untouched, so only the bytes tell the proof apart."""
    path, payload, _ = _published_page(store)
    with preview_store(store.root, store.handle), request_scope(_external()):
        assert egress.filter_withheld_entries(store.root, payload) == payload
        warm = len(renders)
        assert warm
        assert egress.filter_withheld_entries(store.root, payload) == payload
        assert len(renders) == warm

        page = store.root / path
        page.write_text(page.read_text() + "\nForged line\n")
        assert egress.filter_withheld_entries(store.root, payload) == {"items": []}
        assert len(renders) > warm


def test_a_committed_row_change_ends_the_proof_of_the_page_it_has_not_yet_published(store, renders):
    """A managed write keeps the other caches, so only the proof itself must notice the new row."""
    path, payload, receipt = _published_page(store)
    with preview_store(store.root, store.handle), request_scope(_external()):
        assert egress.filter_withheld_entries(store.root, payload) == payload
        store.update_record(CID, item_key=KEY, changes={"title": "Changed"},
                            expected_container_hash=receipt["after_container_hash"],
                            expected_item_version=receipt["after_item_hash"], why="correct")
        assert egress.filter_withheld_entries(store.root, payload) == {"items": []}
        store.reconcile_views()
        assert egress.filter_withheld_entries(store.root, payload) == payload


def test_a_policy_or_audience_change_flips_the_verdict_while_the_proof_stays_warm(store, renders):
    """The proof is about bytes; who may read them is decided again on every request."""
    path, payload, _ = _published_page(store)
    with preview_store(store.root, store.handle):
        with request_scope(_external()):
            assert egress.filter_withheld_entries(store.root, payload) == payload
        warm = len(renders)

        write_scope(store.root, paths=path.removeprefix("Knowledge Base/"))
        write_rule(store.root, ceiling=0)
        with request_scope(_external()):
            assert egress.filter_withheld_entries(store.root, payload) == {"items": []}
        with request_scope(owner_principal()):
            assert egress.filter_withheld_entries(store.root, payload) == payload
        assert len(renders) == warm


def test_a_commit_from_another_connection_ends_the_proof_though_the_cache_restamps_past_it(
    store, renders, monkeypatch
):
    """A rolled-back write that changed no rows restamps the cache after its ROLLBACK. A commit from
    another connection landing just before that restamp is then invisible to the cache's own check,
    so the proof has to notice it from the store stamp it was made at.
    """
    path, payload, _ = _published_page(store)
    with preview_store(store.root, store.handle), request_scope(_external()):
        assert egress.filter_withheld_entries(store.root, payload) == payload
        warm = len(renders)

        cache, finish, elsewhere = store.handle.release_cache, store.handle.release_cache.finish, []

        def commit_elsewhere_then_finish(committed):
            if not committed and not elsewhere:
                other = sqlite3.connect(store.handle.path, timeout=5, isolation_level=None)
                try:
                    other.execute("UPDATE items SET body=? WHERE view_path=?", ("Edited elsewhere", path))
                finally:
                    other.close()
                elsewhere.append(path)
            return finish(committed)

        monkeypatch.setattr(cache, "finish", commit_elsewhere_then_finish)
        with pytest.raises(RuntimeError), store.handle.transaction():
            raise RuntimeError("a write that changes no rows")
        assert elsewhere

        assert egress.filter_withheld_entries(store.root, payload) == {"items": []}
        assert len(renders) > warm


def test_a_tombstone_written_then_removed_is_seen_by_the_next_decision(store, renders):
    path, payload, _ = _published_page(store)
    write_scope(store.root, paths=path.removeprefix("Knowledge Base/"))
    write_rule(store.root, ceiling=0)
    with preview_store(store.root, store.handle), request_scope(owner_principal()):
        assert egress.filter_withheld_entries(store.root, payload) == payload
        assert path not in lifecycle.tombstoned_paths(store.root)
        operation = lifecycle.begin_deletion(
            store.root, source_rel=path, trash_rel="Knowledge Base/_trash/current.md",
        )
        assert operation.governed
        assert path in lifecycle.tombstoned_paths(store.root)
        assert egress.filter_withheld_entries(store.root, payload) == {"items": []}
        lifecycle.abort_deletion(operation)
        assert path not in lifecycle.tombstoned_paths(store.root)
        assert egress.filter_withheld_entries(store.root, payload) == payload


def test_lost_event_evidence_fails_a_standing_tombstone_closed(store):
    """A marker is trusted only with its intent event; losing the log must not keep serving the old set."""
    path, _payload, _ = _published_page(store)
    write_scope(store.root, paths=path.removeprefix("Knowledge Base/"))
    write_rule(store.root, ceiling=0)
    with preview_store(store.root, store.handle), request_scope(owner_principal()):
        lifecycle.begin_deletion(store.root, source_rel=path, trash_rel="Knowledge Base/_trash/current.md")
        standing = lifecycle.tombstoned_paths(store.root)
        assert path in standing and lifecycle.FAIL_CLOSED_TOMBSTONE not in standing

        events = list((store.root / "Knowledge Base/_Governance/events").glob("*/*.jsonl"))
        assert events
        for log in events:
            log.unlink()
        assert lifecycle.FAIL_CLOSED_TOMBSTONE in lifecycle.tombstoned_paths(store.root)


def test_a_linked_governance_directory_fails_closed_though_no_marker_exists(store):
    """With no marker file the cached empty set stands on the directories alone; a link there must end it."""
    _published_page(store)
    write_scope(store.root)
    governance = store.root / "Knowledge Base/_Governance"
    with preview_store(store.root, store.handle), request_scope(owner_principal()):
        assert lifecycle.tombstoned_paths(store.root) == frozenset()
        governance.rename(governance.with_name("_Governance.real"))
        governance.symlink_to(governance.with_name("_Governance.real"), target_is_directory=True)
        assert lifecycle.FAIL_CLOSED_TOMBSTONE in lifecycle.tombstoned_paths(store.root)
