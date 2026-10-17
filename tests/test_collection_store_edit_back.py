"""Edit-back of store collection views (OpenSpec move-structured-collections-to-sqlite §6, §16 A1, A2).

The writer-level cases drive ``edit_back.EditBack`` with explicit monotonic times, as the
store thread does without a file watcher; the service cases drive the installed service.
"""

import hashlib
import json

import pytest
from test_collection_store_migration import env as env
from test_collection_store_service import service as service
from test_collection_store_writer import CID, KEY, create, manifest_path, manifest_text
from test_collection_store_writer import store as store

from exomem import get_page
from exomem import structured_collections as collections
from exomem.collection_store import capability, edit_back

SETTLED = edit_back.SETTLE_SECONDS
HELD = edit_back.INVALID_SECONDS
VIEWS = capability.COLLECTION_VIEWS_V1


@pytest.fixture(autouse=True)
def views_released(monkeypatch):
    """Each case runs under a release that enables edit-back, unless it sets its own release."""
    monkeypatch.setattr(capability, "RELEASED", capability.RELEASED | {VIEWS})


def passes(store, *times):
    """One edit-back runner, run once at each monotonic time."""
    runner = edit_back.EditBack()
    for now in times:
        runner.run(store, now=now)
    return runner


def txns(store, operation=None):
    query = "SELECT operation,actor,request_id FROM txns" + (" WHERE operation=?" if operation else "")
    return store.connection.execute(query, (operation,) if operation else ()).fetchall()


def holds(store):
    return store.connection.execute(
        "SELECT held_id,code,held_bytes FROM held_candidates WHERE kind='view-correction' ORDER BY held_id"
    ).fetchall()


def appended(store, **values):
    create(store)
    receipt = store.append_record(CID, item={"title": "One", **values}, item_key=KEY, why="observe")
    return receipt, store.root / receipt["affected_paths"][0]


def test_a_settled_valid_edit_is_one_view_edit_and_a_replay_commits_nothing(store):
    """Defect: a valid edit of the current view is deferred forever, or is applied twice."""
    receipt, path = appended(store, count=1)
    edited = path.read_bytes().replace(b"title: One", b"title: Edited").replace(b"count: 1", b"count: 2")
    path.write_bytes(edited)
    runner = passes(store, 0.0, SETTLED)
    assert txns(store, "view_edit") == [("view_edit", "owner:view-edit", edit_back.request_id(
        receipt["affected_paths"][0], 1, hashlib.sha256(edited).hexdigest()))]
    row = store.connection.execute("SELECT row_version,values_json FROM items").fetchone()
    assert row == (2, json.dumps({"count": 2, "title": "Edited"}, separators=(",", ":")))
    assert get_page.get_page(store.root, path=receipt["affected_paths"][0]).frontmatter["exomem_view"]["v"] == 2
    assert holds(store) == [] and not list(path.parent.glob(".exomem-collection-aside-*"))
    # The same bytes again, as after a crash before their render was installed: nothing new.
    rendered = path.read_bytes()
    path.write_bytes(edited)
    runner.run(store, now=SETTLED + 60)
    runner.run(store, now=2 * SETTLED + 60)
    assert len(txns(store, "view_edit")) == 1 and holds(store) == [] and path.read_bytes() == rendered


def test_an_invalid_save_is_held_once_and_stays_in_place(store):
    """Defect: one invalid save gives a second hold on the next reconcile pass, or loses its bytes."""
    _, path = appended(store, count=1)
    invalid = path.read_bytes().replace(b"count: 1", b"count: broken")
    path.write_bytes(invalid)
    store.reconcile_views()
    store.reconcile_views()
    assert [(code, raw) for _, code, raw in holds(store)] == [("VIEW_INVALID", invalid)]
    assert path.read_bytes() == invalid  # PROVISIONAL O2: kept in place until discarded


def test_an_invalid_view_waiting_to_be_held_does_not_spin_the_store_thread(store):
    """Defect: between settling and being held, an invalid view asks for a pass at once, every pass."""
    _, path = appended(store, count=1)
    path.write_bytes(path.read_bytes().replace(b"count: 1", b"count: broken"))
    runner = passes(store, 0.0)
    assert runner.run(store, now=SETTLED) == pytest.approx(HELD - SETTLED)
    assert holds(store) == []
    assert runner.run(store, now=HELD) > 0 and [code for _, code, _ in holds(store)] == ["VIEW_INVALID"]


def test_a_deleted_view_is_held_and_rendered_again(store):
    """Defect: a deleted view is silently rendered again, so its owner never learns no delete exists."""
    receipt, path = appended(store)
    store.reconcile_views()
    path.unlink()
    store.reconcile_views()
    assert [code for _, code, _ in holds(store)] == ["VIEW_DELETED"]
    assert get_page.get_page(store.root, path=receipt["affected_paths"][0]).frontmatter["title"] == "One"


def test_discard_resolves_a_view_correction_and_renders_over_its_bytes(store):
    """Defect: a held view correction cannot be discarded (C10), or its discarded bytes stay at the view."""
    receipt, path = appended(store, count=1)
    rendered = path.read_bytes()
    path.write_bytes(rendered.replace(b"count: 1", b"count: broken"))
    store.reconcile_views()
    [(held, code, _)] = holds(store)
    assert code == "VIEW_INVALID"
    discarded = store.discard_held(CID, held=held, why="drop the broken edit")
    assert discarded["outcome"] == "discarded" and holds(store) == []
    assert path.read_bytes() == rendered and not list(path.parent.glob(".exomem-collection-aside-*"))
    with pytest.raises(collections.CollectionError, match="HELD_NOT_FOUND"):
        store.discard_held(CID, held=held, why="again")


def test_a_planning_hierarchy_violation_is_held_not_applied(store):
    """Defect: an edit that breaks the Planning hierarchy is classified without the graph and never held."""
    from exomem import vault

    create(store, "planning")
    receipt = store.append_record(CID, item={"title": "Child"}, item_key=KEY, why="capture")
    path = store.root / receipt["affected_paths"][0]
    frontmatter, body, _ = vault.parse_frontmatter(path.read_text(), strict=True)
    # A committed work item needs a parent; this one has none.
    frontmatter.update(status="planned", commitment="committed", horizon="week")
    edited = ("---\n" + vault.serialize_frontmatter(frontmatter) + "\n---\n" + body).encode()
    path.write_bytes(edited)
    passes(store, 0.0, SETTLED, HELD)
    assert txns(store, "view_edit") == []
    [(_, code, raw)] = holds(store)
    assert (code, raw) == ("VIEW_INVALID", edited)
    diagnostics = json.loads(store.connection.execute("SELECT diagnostics_json FROM held_candidates").fetchone()[0])
    assert diagnostics[0]["issues"][0]["code"] == "INVALID_PLAN_RELATION"
    # Planning inspection names the correction while keeping its exact key set (design §8 C4).
    from exomem.collection_store.preview import preview_store
    from exomem.plan_memory import plan_memory

    with preview_store(store.root, store.handle):
        inspected = plan_memory(store.root, "inspect", collection=CID)
    assert "VIEW_INVALID" in {diagnostic["code"] for diagnostic in inspected["diagnostics"]}
    assert "projection" not in inspected and "coverage" not in inspected


def test_without_the_edit_back_release_a_hierarchy_violation_waits_on_disk(store, monkeypatch):
    """Defect: with edit-back unreleased, a Planning edit that breaks the hierarchy is held and its
    view rendered again, where a store before edit-back left it waiting on disk."""
    from exomem import vault

    monkeypatch.setattr(capability, "RELEASED", capability.RELEASED - {VIEWS})
    create(store, "planning")
    receipt = store.append_record(CID, item={"title": "Child"}, item_key=KEY, why="capture")
    path = store.root / receipt["affected_paths"][0]
    frontmatter, body, _ = vault.parse_frontmatter(path.read_text(), strict=True)
    frontmatter.update(status="planned", commitment="committed", horizon="week")  # committed, with no parent
    edited = ("---\n" + vault.serialize_frontmatter(frontmatter) + "\n---\n" + body).encode()
    path.write_bytes(edited)
    store.reconcile_views()
    store.reconcile_views()
    assert holds(store) == [] and path.read_bytes() == edited
    assert store.connection.execute("SELECT state FROM projection_state WHERE path=?",
                                    (receipt["affected_paths"][0],)).fetchone() == ("pending",)


def test_a_manifest_view_edit_becomes_a_revise(store):
    """Defect: an edit of the manifest view is ignored, or bypasses the revise rules."""
    create(store)
    path = store.root / manifest_path()
    path.write_bytes(path.read_bytes().replace(b"title: Work", b"title: Revised work"))
    passes(store, 0.0, SETTLED)
    assert txns(store, "view_edit") == [("view_edit", "owner:view-edit", txns(store, "view_edit")[0][2])]
    version, text = store.connection.execute(
        "SELECT m.manifest_version,m.manifest_text FROM collections c JOIN collection_manifests m "
        "ON m.collection_id=c.collection_id AND m.manifest_version=c.manifest_version").fetchone()
    assert version == 2 and "title: Revised work" in text and "exomem_view" not in text
    assert get_page.get_page(store.root, path=manifest_path()).frontmatter["exomem_view"]["v"] == 2


def test_a_formatting_only_edit_is_rendered_again_without_a_transaction(store):
    """Defect: re-quoting a value or reordering properties commits a transition or leaves the edit."""
    from exomem import vault

    _, path = appended(store, count=1)
    rendered = path.read_bytes()
    frontmatter, body, _ = vault.parse_frontmatter(rendered.decode(), strict=True)
    reordered = dict(reversed(list(frontmatter.items())))
    path.write_bytes(("---\n" + vault.serialize_frontmatter(reordered) + "\n---\n" + body).encode())
    before = txns(store)
    passes(store, 0.0, SETTLED)
    assert txns(store) == before and holds(store) == []
    assert path.read_bytes() == rendered


def test_a_typing_storm_leaves_no_hold(store):
    """Defect: an editor's autosaves, invalid between keystrokes, are held before the owner finishes."""
    _, path = appended(store, count=1)
    rendered = path.read_bytes()
    runner = edit_back.EditBack()
    now = 0.0
    for keystroke in range(12):  # 18 s of autosaves, each followed by a half-second pause
        path.write_bytes(rendered.replace(b"count: 1", f"count: 1{keystroke}x".encode()))
        runner.run(store, now=now)
        runner.run(store, now=now + 0.5)
        now += 1.5
    assert holds(store) == []
    path.write_bytes(rendered.replace(b"count: 1", b"count: 12"))
    runner.run(store, now=now)
    runner.run(store, now=now + 2.0)
    assert holds(store) == [] and len(txns(store, "view_edit")) == 1
    assert json.loads(store.connection.execute("SELECT values_json FROM items").fetchone()[0])["count"] == 12


# The installed service, no watcher: a migrated vault whose Records collection the store serves.

WORK = manifest_path()
SECRET_TAGS = "    tags: {type: array, items: {type: string}}\n"


class Served:
    """A migrated vault and the service serving it, which a test may stop and start again."""

    def __init__(self, root):
        self.root = root
        self.service = None

    def start(self):
        from test_collection_store_migration import start_service

        self.service = start_service(self.root)

    def stop(self):
        self.service.stop()
        self.service = None


@pytest.fixture
def served(env, capsys, monkeypatch):
    from test_collection_store_migration import migrate, stop_writer

    from exomem import records
    from exomem.init import init_vault

    # A full pass every few tenths of a second, so a case need not wait the production 60 s.
    monkeypatch.setattr(edit_back, "FULL_PASS_SECONDS", 0.3)
    root = env
    init_vault(root)
    records.create_collection(root, WORK, manifest_text().replace("    count:", SECRET_TAGS + "    count:"),
                              why="file fixture", scaffold=True)
    stop_writer()
    assert migrate(root, "apply") == 0
    monkeypatch.setattr(capability, "RELEASED", capability.RELEASED | {VIEWS})  # env set its own release
    capsys.readouterr()
    vault = Served(root)
    vault.start()
    try:
        yield vault
    finally:
        if vault.service is not None:
            vault.stop()


def call_as(principal, root, tool, **arguments):
    from exomem import commands, writer_lease
    from exomem.governance.principal import request_scope

    command = next(command for command in commands.product_commands_for("mcp") if command.name == tool)
    with request_scope(principal):
        return writer_lease.invoke_command(command, root, **arguments)


def owner(root, tool, **arguments):
    from exomem.governance.principal import owner_principal

    return call_as(owner_principal(surface="mcp"), root, tool, **arguments)


def store_rows(root, sql, parameters=()):
    from contextlib import closing

    from exomem.collection_store import connection

    with closing(connection.open_reader(connection.store_path(root))) as reader:
        return reader.execute(sql, parameters).fetchall()


def until(predicate, seconds=20):
    from test_collection_store_s1_gate import _until

    _until(predicate, seconds)


def published_view(root, title, **values):
    """Append through the service and wait until the tick has confirmed its view current."""
    receipt = owner(root, "record_memory", action="append", collection=CID, item={"title": title, **values},
                    why="capture")
    relative = receipt["affected_paths"][0]
    until(lambda: store_rows(root, "SELECT state FROM projection_state WHERE path=?", (relative,)) == [("current",)])
    return relative, root / relative


def counted(root, title):
    rows = owner(root, "record_memory", action="query", collection=CID)["rows"]
    return next(row.get("count") for row in rows if row["title"] == title)


def test_an_on_disk_item_edit_becomes_one_governed_view_edit_and_a_stale_save_is_held(served):
    """Defect: the installed service never applies an edit made in a view, applies it twice, loses a
    stale-buffer save, or holds it where inspect, attention and discard cannot reach it."""
    root = served.root
    relative, path = published_view(root, "Served", count=1)
    first = path.read_bytes()
    edited = first.replace(b"count: 1", b"count: 7")
    path.write_bytes(edited)
    until(lambda: counted(root, "Served") == 7)
    edits = "SELECT operation,actor FROM txns WHERE operation='view_edit'"
    assert store_rows(root, edits) == [("view_edit", "owner:view-edit")]
    until(lambda: owner(root, "read_memory", path=relative)["frontmatter"]["count"] == 7)
    rendered = path.read_bytes()
    path.write_bytes(edited)  # the same save again
    until(lambda: path.read_bytes() == rendered)
    assert len(store_rows(root, edits)) == 1
    path.write_bytes(first.replace(b"count: 1", b"count: 9"))  # a stale buffer, stamped before the edit
    until(lambda: owner(root, "record_memory", action="inspect", collection=CID)["projection"]
          ["held_view_corrections"] == 1)
    inspected = owner(root, "record_memory", action="inspect", collection=CID)
    assert "VIEW_CONFLICT" in {diagnostic["code"] for diagnostic in inspected["diagnostics"]}
    held = inspected["coverage"]["held_refs"][0]["held_id"]
    attention = owner(root, "review_memory", mode="attention")  # the default union
    assert held in json.dumps(attention)
    until(lambda: path.read_bytes() == rendered)  # the view shows the store again
    owner(root, "record_memory", action="discard", collection=CID, held=held, why="a stale buffer")
    assert owner(root, "record_memory", action="inspect", collection=CID)["projection"]["held_view_corrections"] == 0
    assert held not in json.dumps(owner(root, "review_memory", mode="attention"))
    assert counted(root, "Served") == 7 and len(store_rows(root, edits)) == 1


def test_an_edit_made_while_the_service_is_stopped_is_applied_after_it_starts(served):
    """Defect: an offline edit is never classified, or the projector overwrites it at startup."""
    root = served.root
    _, path = published_view(root, "Offline", count=1)
    served.stop()
    path.write_bytes(path.read_bytes().replace(b"count: 1", b"count: 4"))
    served.start()
    until(lambda: counted(root, "Offline") == 4)
    assert store_rows(root, "SELECT COUNT(*) FROM held_candidates") == [(0,)]


def test_attention_for_a_withheld_view_correction_equals_attention_without_it(served):
    """Defect: the attention queue names a held correction of a row the caller cannot see."""
    from test_governance_egress import _external, write_rule, write_scope

    from exomem.governance.principal import owner_principal, request_scope

    root = served.root
    published_view(root, "Public", count=1)
    _, path = published_view(root, "Secret", count=1, tags=["secret"])
    write_scope(root, paths="Unrelated/**")
    scope = root / "Knowledge Base/_Governance/scopes/patterns.yaml"
    scope.write_text(scope.read_text() + "tags: [secret]\n")
    write_rule(root, ceiling=0)

    def queue(principal):
        # The audit category the queue reads, and the queue the caller receives.
        with request_scope(principal):
            found = edit_back.attention_findings(root)
        return found, call_as(principal, root, "review_memory", mode="attention",
                              categories=["collection_view_corrections"])

    first = path.read_bytes()
    path.write_bytes(first.replace(b"count: 1", b"count: 2"))
    until(lambda: counted(root, "Secret") == 2)
    absent = queue(_external())
    path.write_bytes(first.replace(b"count: 1", b"count: 3"))  # stale: stamped before the edit
    until(lambda: owner(root, "record_memory", action="inspect", collection=CID)["projection"]
          ["held_view_corrections"] == 1)
    assert queue(_external()) == absent and absent[0] == ([], None)
    assert "VIEW_CONFLICT" in json.dumps(queue(owner_principal(surface="mcp"))[1])


def test_a_held_stale_buffer_save_resumes_as_the_owners_edit_over_the_current_row(store):
    """Defect: a held VIEW_CONFLICT cannot be resumed, resuming reverts the newer row, or a refused
    resume drops the owner's bytes."""
    receipt, path = appended(store, count=1)
    store.reconcile_views()
    stale = path.read_bytes().replace(b"title: One", b"title: Owner")
    agent = store.update_record(CID, item_key=KEY, changes={"count": 2}, why="agent",
                                expected_container_hash=receipt["after_container_hash"],
                                expected_item_version=receipt["after_item_hash"])
    path.write_bytes(stale)
    passes(store, 0.0, SETTLED)
    [(held, code, raw)] = holds(store)
    assert (code, raw) == ("VIEW_CONFLICT", stale)
    with pytest.raises(collections.CollectionError, match="SCHEMA_FIELD_TYPE"):
        store.update_record(CID, item_key=KEY, held=held, changes={"count": "bad"}, why="resume",
                            expected_container_hash=agent["after_container_hash"],
                            expected_item_version=agent["after_item_hash"])
    assert holds(store) == [(held, "VIEW_CONFLICT", stale)]
    store.update_record(CID, item_key=KEY, held=held, why="keep the owner's title",
                        expected_container_hash=agent["after_container_hash"],
                        expected_item_version=agent["after_item_hash"])
    assert holds(store) == []
    assert json.loads(store.connection.execute("SELECT values_json FROM items").fetchone()[0]) == {
        "count": 2, "title": "Owner"}


def test_an_edit_inside_the_managed_presentation_block_is_not_adopted(store):
    """Defect: text typed into the generated presentation block becomes the item's authored body."""
    text = manifest_text().removesuffix("---\n") + "item_presentation:\n  version: 1\n  title: title\n  summary: [count]\n---\n"
    store.create_collection(manifest_path(), text, why="create", scaffold=False)
    receipt = store.append_record(CID, item={"title": "One", "count": 1}, item_key=KEY, why="capture",
                                  body="Authored line\n")
    path = store.root / receipt["affected_paths"][0]
    rendered = path.read_bytes()
    assert b"- **Count:** 1\n" in rendered
    path.write_bytes(rendered.replace(b"- **Count:** 1\n", b"- **Count:** 99\n"))
    before = txns(store)
    passes(store, 0.0, SETTLED)
    assert txns(store) == before and holds(store) == [] and path.read_bytes() == rendered
    assert store.connection.execute("SELECT body FROM items").fetchone() == ("Authored line\n",)


def test_the_stores_own_render_is_left_alone_and_then_skipped_by_its_stat(store, monkeypatch):
    """Defect: the store's own render is taken for an edit, or every pass reads every view again."""
    from exomem import held_fs

    _, path = appended(store, count=1)
    store.reconcile_views()
    rendered, before = path.read_bytes(), txns(store)
    with held_fs.acquire(store.root).require() as fs:
        platform = type(fs)
    reads = []
    read = platform.read
    monkeypatch.setattr(platform, "read", lambda fs, file: reads.append(file) or read(fs, file))
    runner = passes(store, 0.0)
    assert len(reads) == 2  # the first pass reads the manifest and item views
    runner.run(store, now=60.0)
    assert len(reads) == 2  # the next full pass only stats them
    assert txns(store) == before and holds(store) == [] and path.read_bytes() == rendered


def test_views_render_no_file_mode_audit_marker_or_head(store):
    """Defect: a store view carries a file-mode audit marker or a migrated manifest's audit head,
    which no store write keeps current and an edit of the view would carry back."""
    from exomem import record_formats

    legacy = record_formats.render_manifest_audit_head(manifest_text(), f"{7:024x}", semantic_profile="records",
                                                       reader_version=1)
    assert "record_audit" in legacy
    store.create_collection(manifest_path(), legacy, why="a migrated manifest", scaffold=False)
    receipt = store.append_record(CID, item={"title": "One"}, item_key=KEY, why="observe")
    assert b"exomem-record-audit" not in (store.root / receipt["affected_paths"][0]).read_bytes()
    manifest = get_page.get_page(store.root, path=manifest_path()).frontmatter
    assert "record_audit" not in manifest and manifest["exomem_view"]["v"] == 1


def vault_bytes(root):
    """Every vault file but the store replica, which publishes after earlier commits on its own schedule."""
    from exomem.collection_store import replica

    skipped = replica.replica_path(root)
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in sorted(root.rglob("*"))
            if path.is_file() and path != skipped}


@pytest.fixture
def quick_service(monkeypatch, request):
    """The summary-collection service, started with edit-back windows of tenths of a second."""
    monkeypatch.setattr(edit_back, "SETTLE_SECONDS", 0.2)
    monkeypatch.setattr(edit_back, "INVALID_SECONDS", 0.4)
    monkeypatch.setattr(edit_back, "FULL_PASS_SECONDS", 0.3)
    return request.getfixturevalue("service")


@pytest.mark.parametrize("released", [frozenset(), frozenset({VIEWS})], ids=["production", "summary-fenced"])
def test_an_unreleased_route_takes_no_view_edit(quick_service, monkeypatch, released):
    """Defect: edit-back commits or holds an owner's edit of a summary collection's view while the
    release keeps edit-back off, or while it keeps every route on that collection refused."""
    import time

    from exomem.collection_store import runtime

    root, view = quick_service.root, quick_service.root / manifest_path()
    until(lambda: store_rows(root, "SELECT COUNT(*) FROM projection_state WHERE state!='current'") == [(0,)])
    monkeypatch.setattr(capability, "RELEASED", released)
    view.write_bytes(view.read_bytes().replace(b"title: Work", b"title: Renamed"))
    before, txns_before = vault_bytes(root), store_rows(root, "SELECT COUNT(*) FROM txns")
    for _ in range(6):  # each served request wakes the store thread's views tick
        runtime._server(root).call(lambda: None)
        time.sleep(0.5)
    assert store_rows(root, "SELECT COUNT(*) FROM txns") == txns_before
    assert store_rows(root, "SELECT COUNT(*) FROM held_candidates") == [(0,)]
    assert vault_bytes(root) == before
    # The same edit is applied once the release serves both edit-back and the collection.
    monkeypatch.setattr(capability, "RELEASED", frozenset({capability.RECORDS_SUMMARY_V1, VIEWS}))
    runtime._server(root).call(lambda: None)
    until(lambda: store_rows(root, "SELECT COUNT(*) FROM txns WHERE operation='view_edit'") == [(1,)])
    [(text,)] = store_rows(root, "SELECT manifest_text FROM collection_manifests ORDER BY manifest_version DESC LIMIT 1")
    assert "title: Renamed" in text


def test_edit_back_waits_for_its_release_on_a_migrated_collection(served, monkeypatch):
    """Defect: edit-back applies an owner's item edit under a release that serves migrated
    collections but keeps edit-back off."""
    import time

    from exomem.collection_store import runtime

    root = served.root
    _, path = published_view(root, "Waiting", count=1)
    monkeypatch.setattr(capability, "RELEASED", capability.RELEASED - {VIEWS})
    path.write_bytes(path.read_bytes().replace(b"count: 1", b"count: 5"))
    for _ in range(6):  # past the settle window; each served request wakes the views tick
        runtime._server(root).call(lambda: None)
        time.sleep(0.5)
    assert counted(root, "Waiting") == 1
    assert store_rows(root, "SELECT COUNT(*) FROM held_candidates") == [(0,)]
    monkeypatch.setattr(capability, "RELEASED", capability.RELEASED | {VIEWS})
    until(lambda: counted(root, "Waiting") == 5)


def test_an_edited_summary_page_is_held_by_the_served_publication_pass(store):
    """Defect: the served publication pass leaves an edited summary page to edit-back, which reads
    only item and manifest views, so the page is never held or rendered again."""
    from test_collection_store_summary import bulk, pages
    from test_collection_store_summary import create as create_summary

    from exomem.governance.principal import owner_principal, request_scope

    with request_scope(owner_principal()):
        create_summary(store)
        bulk(store, [{"title": "One", "count": 1}])
        store.reconcile_views()
        store.reconcile_views()
        page = store.root / pages(store)[0]
        edited = page.read_bytes() + b"\n- title: Two\n  count: 2\n"
        page.write_bytes(edited)
        bulk(store, [{"title": "Three", "count": 3}])
        for _ in range(6):
            store.reconcile_views(pending_only=True)
    assert [(code, raw) for _, code, raw in holds(store)] == [("SUMMARY_VIEW_READ_ONLY", edited)]
    assert page.read_bytes() != edited


def test_reordering_a_manifest_views_properties_commits_nothing(store):
    """Defect: a formatting-only manifest edit commits a revise, which also refuses an in-flight
    revise that carried the earlier manifest hash."""
    from exomem import vault

    create(store)
    store.reconcile_views()
    path = store.root / manifest_path()
    rendered = path.read_bytes()
    frontmatter, body, _ = vault.parse_frontmatter(rendered.decode(), strict=True)
    reordered = {name: frontmatter[name] for name in reversed(list(frontmatter))}
    edited = ("---\n" + vault.serialize_frontmatter(reordered) + "\n---\n" + body).encode()
    assert edited != rendered
    path.write_bytes(edited)
    before = txns(store)
    passes(store, 0.0, SETTLED)
    assert txns(store) == before and holds(store) == [] and path.read_bytes() == rendered


def test_a_view_correction_held_under_an_earlier_id_shape_can_be_discarded(store):
    """Defect: a correction an earlier release held under a 24-hex id is listed for attention but
    every discard answers HELD_NOT_FOUND."""
    _, path = appended(store, count=1)
    path.write_bytes(path.read_bytes().replace(b"count: 1", b"count: broken"))
    store.reconcile_views()
    [(held, _, _)] = holds(store)
    legacy = hashlib.sha256(held.encode()).hexdigest()[:24]
    store.connection.execute("UPDATE held_candidates SET held_id=? WHERE held_id=?", (legacy, held))
    store.connection.commit()
    store.discard_held(CID, held=legacy, why="an older hold")
    assert holds(store) == []


# Log views: one view of every row, edited block by block (design §6, §16 A2).


def test_an_edit_of_a_stale_log_block_is_held_as_a_conflict(store):
    """Defect: a log block saved from a buffer older than its row reverts the agent's newer row, or
    the owner's bytes for that block are lost when the log is rendered again."""
    from test_collection_store_writer import log_manifest_text

    store.create_collection(manifest_path(), log_manifest_text(), why="create")
    receipt = store.append_record(CID, item={"title": "One", "details": [{"field": "reps", "value": "10"}]},
                                  item_key=KEY, why="capture")
    store.reconcile_views()
    path = store.root / receipt["affected_paths"][0]
    stale = path.read_bytes()
    store.update_record(CID, item_key=KEY, changes={"details": [{"field": "reps", "value": "11"}]}, why="agent",
                        expected_container_hash=receipt["after_container_hash"],
                        expected_item_version=receipt["after_item_hash"])
    store.reconcile_views()
    edited = stale.replace(b"- reps | 10", b"- reps | 12")
    path.write_bytes(edited)
    passes(store, 0.0, SETTLED)
    [(_, code, raw)] = holds(store)
    assert code == "VIEW_CONFLICT" and b"- reps | 12" in raw
    assert json.loads(store.connection.execute("SELECT values_json FROM items").fetchone()[0])["details"] == [
        {"field": "reps", "value": "11"}]
    assert txns(store, "view_edit") == [] and b"- reps | 11" in path.read_bytes()


def test_an_imported_log_edited_before_its_first_render_is_held_and_says_how_to_resume(store, tmp_path):
    """Defect: an edit of an imported log that the store never rendered, so it carries no stamps, is
    read as an edit of the current rows, or the first render drops it without a word."""
    from test_collection_store_views import X3_ID, imported_log

    source = imported_log(store, tmp_path)
    edited = source.read_bytes().replace(b"Overhead Press | blue short | 12", b"Overhead Press | blue short | 13")
    source.write_bytes(edited)
    passes(store, 0.0, SETTLED, HELD + SETTLED)
    [(_, code, raw)] = holds(store)
    assert (code, raw) == ("VIEW_INVALID", edited)
    [diagnostic] = json.loads(store.connection.execute("SELECT diagnostics_json FROM held_candidates").fetchone()[0])
    assert [issue["code"] for issue in diagnostic["issues"]] == ["VIEW_UNSTAMPED"]
    assert diagnostic["issues"][0]["remedy"]
    assert txns(store, "view_edit") == []
    assert b"exomem-view" in source.read_bytes()
    assert store.connection.execute("SELECT COUNT(*) FROM items WHERE collection_id=?", (X3_ID,)).fetchone()[0] > 0


@pytest.mark.parametrize("stamped", ["foreign", "tampered"])
def test_a_log_blocks_foreign_or_tampered_stamp_is_held(store, stamped):
    """Defect: a log block copied from another store's view, or one whose stamp names another
    payload, is applied as the owner's edit of the current row."""
    from test_collection_store_writer import log_manifest_text

    from exomem.collection_store import connection

    store.create_collection(manifest_path(), log_manifest_text(), why="create")
    receipt = store.append_record(CID, item={"title": "One", "details": [{"field": "reps", "value": "10"}]},
                                  item_key=KEY, why="capture")
    store.reconcile_views()
    path = store.root / receipt["affected_paths"][0]
    rendered = path.read_bytes()
    instance = store.connection.execute("SELECT value FROM store_meta WHERE key='instance_id'").fetchone()[0]
    payload = store.connection.execute("SELECT payload_hash FROM items").fetchone()[0]
    tampered = (f"i={instance}", "i=00000000-0000-4000-8000-000000000001") if stamped == "foreign" \
        else (f"h={payload[:12]}", "h=000000000000")
    edited = rendered.replace(b"- reps | 10", b"- reps | 12").replace(*(part.encode() for part in tampered))
    path.write_bytes(edited)
    passes(store, 0.0, SETTLED, HELD + SETTLED)
    [(_, code, raw)] = holds(store)
    assert txns(store, "view_edit") == []
    if stamped == "foreign":
        assert (code, raw) == ("VIEW_FOREIGN", edited)
        with pytest.raises(connection.CollectionStoreError, match="COLLECTION_STORE_DIVERGED"):
            store.append_record(CID, item={"title": "Two", "details": []}, why="capture")
    else:
        assert code == "VIEW_INVALID" and b"- reps | 12" in raw and b"h=000000000000" in raw
        assert b"- reps | 10" in path.read_bytes()


def held_issues(store):
    return [issue["code"] for diagnostic in json.loads(
        store.connection.execute("SELECT diagnostics_json FROM held_candidates").fetchone()[0])
        for issue in diagnostic.get("issues", ())]


def test_a_log_block_edit_that_also_adds_text_is_held_with_the_text(store):
    """Defect: a block edit that changes a value and adds a line of prose applies the value and
    drops the prose when the log is rendered again."""
    from test_collection_store_writer import log_manifest_text

    store.create_collection(manifest_path(), log_manifest_text(), why="create")
    receipt = store.append_record(CID, item={"title": "One", "details": [{"field": "reps", "value": "10"}]},
                                  item_key=KEY, why="capture")
    store.reconcile_views()
    path = store.root / receipt["affected_paths"][0]
    path.write_bytes(path.read_bytes().replace(b"- reps | 10\n", b"- reps | 12\nFelt strong today.\n"))
    passes(store, 0.0, SETTLED, HELD + SETTLED)
    assert txns(store, "view_edit") == []
    [(_, code, raw)] = holds(store)
    assert code == "VIEW_INVALID" and b"- reps | 12\nFelt strong today.\n" in raw
    assert held_issues(store) == ["VIEW_BLOCK_TEXT"]
    assert json.loads(store.connection.execute("SELECT values_json FROM items").fetchone()[0])["details"] == [
        {"field": "reps", "value": "10"}]


def test_a_log_block_that_names_no_row_is_held_and_appends_nothing(store):
    """Defect: a block the owner typed into the log, which no record id binds to a row, is dropped
    by the next render, or becomes a row without a governed append."""
    from test_collection_store_writer import log_manifest_text

    store.create_collection(manifest_path(), log_manifest_text(), why="create")
    receipt = store.append_record(CID, item={"title": "One", "details": []}, item_key=KEY, why="capture")
    store.reconcile_views()
    path = store.root / receipt["affected_paths"][0]
    typed = b"### Two\n- reps | 5\n\n"
    path.write_bytes(path.read_bytes().replace(b"## Entries\n", b"## Entries\n" + typed, 1))
    before = txns(store)
    passes(store, 0.0, SETTLED)
    assert [(code, raw) for _, code, raw in holds(store)] == [("VIEW_UNBOUND", typed)]
    assert txns(store) == before and b"### Two" not in path.read_bytes()


def test_a_held_whole_log_view_is_withheld_from_a_caller_who_cannot_read_every_row(store):
    """Defect: a hold of the whole log view, whose bytes show every row, is released to a caller
    from whom one of those rows is withheld."""
    from test_collection_store_writer import OTHER, log_manifest_text
    from test_governance_egress import _external, write_rule, write_scope

    from exomem.collection_store import authority
    from exomem.collection_store.preview import preview_store
    from exomem.governance.principal import owner_principal, request_scope

    store.create_collection(manifest_path(), log_manifest_text(), why="create")
    store.append_record(CID, item={"title": "Open", "details": []}, item_key=KEY, why="capture")
    receipt = store.append_record(CID, item={"title": "Secret", "details": []}, item_key=OTHER, why="capture")
    store.reconcile_views()
    log = receipt["affected_paths"][0]
    path = store.root / log
    path.write_bytes(b"A note above the log.\n" + path.read_bytes())  # an edit of the frame holds the whole view
    passes(store, 0.0, SETTLED, HELD + SETTLED)
    assert [code for _, code, _ in holds(store)] == ["VIEW_INVALID"] and held_issues(store) == ["VIEW_FRAME_CHANGED"]
    sid = store.connection.execute("SELECT value FROM store_meta WHERE key='store_id'").fetchone()[0]
    marker = authority.marker_path(store.root)
    marker.parent.mkdir(parents=True)
    marker.write_text(json.dumps({
        "version": 2, "mode": "store", "default_authority": "file", "store_id": sid, "authority_epoch": 1,
        "collections": [{"collection_id": CID, "manifest_path": manifest_path(), "authority": "store",
                         "store_id": sid, "semantic_profile": "records", "source_path": log,
                         "layout": "markdown-log"}],
        "collection_store_fence": {"capability": "collections-store-v1", "generation": 1},
    }))
    write_scope(store.root, paths="Unrelated/**")
    scope = store.root / "Knowledge Base/_Governance/scopes/patterns.yaml"
    scope.write_text(scope.read_text() + f"refs: [exomem://record/{CID}/{OTHER}]\n")  # withholds one row by its reference
    write_rule(store.root, ceiling=0)

    [held] = store.connection.execute("SELECT held_id FROM held_candidates").fetchone()

    def attention(who):
        with preview_store(store.root, store.handle), request_scope(who):
            findings, unknown = edit_back.attention_findings(store.root)
        return [finding.meta["held_id"] for finding in findings], unknown

    assert attention(owner_principal()) == ([held], None)
    assert attention(_external()) == ([], None)


X3 = "Knowledge Base/Records/Health/X3/_collection.md"
X3_LOG = "Knowledge Base/Records/Health/X3/Training Log.md"


@pytest.fixture
def served_log(env, capsys, monkeypatch):
    """A migrated vault whose X3 log has a BOM, CRLF lines and a section after its own, served."""
    from record_fixtures import copy_x3_fixture
    from test_collection_store_migration import migrate, stop_writer

    from exomem.init import init_vault

    monkeypatch.setattr(edit_back, "FULL_PASS_SECONDS", 0.3)
    root = env
    init_vault(root)
    copy_x3_fixture(root)
    log = root / X3_LOG
    log.write_bytes(b"\xef\xbb\xbf" + log.read_bytes().replace(b"\n", b"\r\n"))
    stop_writer()
    assert migrate(root, "apply") == 0
    monkeypatch.setattr(capability, "RELEASED", capability.RELEASED | {VIEWS})
    capsys.readouterr()
    vault = Served(root)
    vault.start()
    try:
        yield vault
    finally:
        if vault.service is not None:
            vault.stop()


def log_state(root):
    return store_rows(root, "SELECT state FROM projection_state WHERE path=?", (X3_LOG,))


def test_a_served_log_view_is_exports_render_and_takes_one_edited_block_as_one_update(served_log):
    """Defect: the projector and the export render a log by two rules that disagree, so the view
    the owner edits is not the file an export writes back; or a saved log view is applied as
    anything but one governed update per edited block, or a block the owner removed is dropped."""
    from contextlib import closing

    from test_collection_store_views import SESSION, SESSION_HEADING

    from exomem import record_formats
    from exomem.collection_store import connection, export

    root = served_log.root
    owner(root, "record_memory", action="append", collection=X3, item=SESSION, why="a store-mode session")
    until(lambda: log_state(root) == [("current",)])
    view = (root / X3_LOG).read_bytes()
    with closing(connection.open_reader(connection.store_path(root))) as conn:
        text, frame = conn.execute(
            "SELECT m.manifest_text,c.log_frame_json FROM collections c JOIN collection_manifests m "
            "ON m.collection_id=c.collection_id AND m.manifest_version=c.manifest_version").fetchone()
        manifest = collections.parse_manifest_bytes(root, X3, text.encode())
        [(_, exported)] = export._render_log(root, conn, manifest, manifest.collection_id, frame)
    # The export writes no stamps, because file mode has no store to name.
    unstamped = b"".join(line for line in view.splitlines(keepends=True)
                         if record_formats.read_log_block_stamp(line) is None)
    assert unstamped == exported and view.startswith(b"\xef\xbb\xbf") and SESSION_HEADING in view
    blocks = view.split(b"### ")
    removed = next(block for block in blocks if block.startswith("2024-10-13 · Pull".encode()))
    edited = view.replace(b"Overhead Press | blue short | 12", b"Overhead Press | blue short | 13")
    (root / X3_LOG).write_bytes(edited.replace(b"### " + removed, b""))
    until(lambda: store_rows(root, "SELECT code FROM held_candidates") == [("VIEW_DELETED",)])
    assert store_rows(root, "SELECT operation,actor FROM txns WHERE operation='view_edit'") == [
        ("view_edit", "owner:view-edit")]
    until(lambda: log_state(root) == [("current",)] and b"### " + removed in (root / X3_LOG).read_bytes())
    assert b"Overhead Press | blue short | 13" in (root / X3_LOG).read_bytes()


def test_stopping_the_service_installs_its_pending_log_view(served_log, monkeypatch):
    """Defect: a stop or a lease handoff leaves a committed append's log view pending, so the vault
    file stays behind the store until another holder starts."""
    from test_collection_store_views import SESSION, SESSION_HEADING

    from exomem.collection_store import runtime

    root = served_log.root
    monkeypatch.setattr(runtime.StoreServer, "_views_tick", lambda self: 3600.0)  # the projector stays idle
    owner(root, "record_memory", action="append", collection=X3, item=SESSION, why="a store-mode session")
    assert log_state(root) == [("pending",)]
    served_log.stop()
    assert SESSION_HEADING in (root / X3_LOG).read_bytes()
