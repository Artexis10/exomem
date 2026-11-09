"""In-place conversion of a populated store collection to summary mode (OpenSpec
move-structured-collections-to-sqlite C2.3 to C2.6, design §17).

Each case starts from a file vault that the existing legacy importer migrates, so the store
holds what production holds: an items-mode Records collection with an item file and a held
candidate, and an items-mode Planning collection. The running service converts one collection
at a time, as the cutover will, and the public tools read the result.
"""

from __future__ import annotations

import hashlib
import json
import time
from contextlib import closing
from pathlib import Path

import pytest
from record_fixtures import LEDGER_COLLECTION_PATH as LEDGER
from record_presentation_fixtures import COLLECTION_PATH as OBSERVED
from record_presentation_fixtures import ITEM_KEY as OBSERVATION
from record_presentation_fixtures import manifest_text as observed_manifest
from record_presentation_fixtures import values as observation
from test_collection_store_migration import (  # noqa: F401 - the env fixture
    PLANNING,
    WORK,
    env,
    legacy_vault,
    migrate,
    start_service,
    stop_writer,
)
from test_collection_store_s1_gate import _until
from test_collection_store_service import OWNER, call, refused
from test_collection_store_writer import CID, KEY, OTHER
from granted_writer import grant_writer
from test_governance_egress import EXTERNAL, SCOPE_ID, write_rule, write_scope

from exomem import commands, records, state_migration, writer_lease
from exomem import structured_collections as collections
from exomem.collection_store import (
    capability,
    connection,
    conversion,
    edit_back,
    export,
    runtime,
    summary,
)
from exomem.governance.principal import RequestPrincipal, request_scope

FLOOR = "collections-sqlite-only-v1"
# The env fixture silences the due-state carrier; a case that reads the due-state projection restores it.
DUE_STATE_CARRIER = records._due_state_carrier
OBSERVED_ID = "44444444-4444-4444-8444-444444444444"
PLAN = "c1c1c1c1-c1c1-4c1c-8c1c-c1c1c1c1c1c1"


def store(root, sql, *args):
    with closing(connection.open_reader(connection.store_path(root))) as reader:
        return reader.execute(sql, args).fetchall()


def stored_rows(root, cid):
    return store(root, "SELECT item_key,row_version,payload_hash FROM items WHERE collection_id=? ORDER BY item_key", cid)


def events(root, path):
    tool = "plan_memory" if path == PLANNING else "record_memory"
    history = call(root, tool, action="query", collection=path, include_agent_history=True)["agent_history"]
    return [event["transition_id"] for event in history["events"]]


def item_files(root, path):
    folder = root / Path(path).parent / "Items"
    return {file.relative_to(root).as_posix(): hashlib.sha256(file.read_bytes()).hexdigest()
            for file in sorted(folder.rglob("*.md")) if not file.name.startswith("_")}


def settled(root, cid):
    with closing(connection.open_reader(connection.store_path(root))) as reader:
        return not summary.publication_outstanding(reader, cid)


def convert(root, collection):
    with request_scope(OWNER):
        return conversion.convert(root, collection, why="cutover to summary storage")


def serve(root, monkeypatch):
    """Start the service on a migrated vault, as the release that ships the cutover runs it."""
    # The cutover ships in a release that serves summary collections and runs edit-back.
    monkeypatch.setattr(capability, "RELEASED", frozenset(
        {capability.COLLECTIONS_MIGRATE_V1, capability.RECORDS_SUMMARY_V1, capability.COLLECTION_VIEWS_V1}))
    monkeypatch.setattr(runtime, "PUBLISH_INTERVAL_SECONDS", 0.1)
    # A full census pass every few tenths of a second, so a new item view need not wait the production 60 s.
    monkeypatch.setattr(edit_back, "FULL_PASS_SECONDS", 0.3)
    return start_service(root)


@pytest.fixture
def served(env, monkeypatch):  # noqa: F811 - the imported fixture, by name
    """A migrated vault under its running service, with one Planning row written in items mode."""
    root = legacy_vault(env)
    assert migrate(root, "apply") == 0
    service = serve(root, monkeypatch)
    try:
        call(root, "plan_memory", action="add", collection=PLANNING, plan_id=PLAN,
             item={"title": "Publish the guide", "kind": "outcome", "status": "planned",
                   "commitment": "committed", "horizon": "quarter"}, why="items-mode row")
        _until(lambda: settled(root, CID) and settled(root, OTHER) and item_files(root, PLANNING), 30)
        yield root
    finally:
        service.stop()


def test_conversion_keeps_every_row_and_moves_the_item_files_into_a_verified_archive(served):
    """Defect: conversion loses or rewrites a row, drops audit history, leaves item files in the
    vault, removes a file whose archived copy does not match, or leaves the collection unwritable."""
    root = served
    before = {cid: (stored_rows(root, cid), events(root, path), item_files(root, path))
              for cid, path in ((CID, WORK), (OTHER, PLANNING))}
    assert all(files for _rows, _events, files in before.values())

    for cid, path in ((CID, WORK), (OTHER, PLANNING)):
        receipt = convert(root, path)
        assert (receipt["outcome"], receipt["phase"]) == ("converted", "complete"), receipt
        rows, history, files = before[cid]
        assert stored_rows(root, cid) == rows
        after = events(root, path)
        # The prior audit events stay; the conversion adds its own revise event.
        assert set(history) < set(after) and len(after) == len(history) + 1
        assert item_files(root, path) == {}
        archive = Path(receipt["archive"])
        assert not archive.is_relative_to(root)
        listed = json.loads((archive / "manifest.json").read_text())
        assert {entry["path"]: entry["sha256"] for entry in listed["files"]} == files
        for entry in listed["files"]:
            assert hashlib.sha256((archive / "files" / entry["path"]).read_bytes()).hexdigest() == entry["sha256"]
        assert store(root, "SELECT view_mode FROM collections WHERE collection_id=?", cid) == [("summary",)]
        # The service publishes the new manifest view after the commit.
        _until(lambda path=path: "strategy: sqlite" in (root / path).read_text(), 30)
    assert FLOOR in state_migration.recorded_descriptor_ids(root)
    # The census passes that published the manifests saw the removals, and none held one as a deleted view.
    assert store(root, "SELECT view_path FROM held_candidates WHERE code=?", edit_back.DELETED) == []
    # File mode cannot hold a converted collection: reverse export keeps it in the store and says why.
    with closing(connection.open_reader(connection.store_path(root))) as reader:
        sealed, _work = export.plan(root, reader)
    blocked = {row["collection_id"]: row.get("blocker", {}).get("code") for row in sealed["collections"]}
    assert (blocked[CID], blocked[OTHER]) == (export.STORE_NATIVE, export.STORE_NATIVE)

    # Reads, updates and transitions keep working on the converted collections.
    work = call(root, "record_memory", action="query", collection=WORK)
    assert [row["title"] for row in work["rows"]] == ["One"]
    updated = call(root, "record_memory", action="update", collection=WORK, item_key=KEY, changes={"count": 7},
                   why="after conversion", expected_container_hash=work["snapshot"],
                   expected_item_version=work["rows"][0]["item_version"])
    assert updated["outcome"] == "committed", updated
    plans = call(root, "plan_memory", action="query", collection=PLANNING)
    triaged = call(root, "plan_memory", action="triage", collection=PLANNING, plan_id=PLAN,
                   transition={"status": "active"}, why="after conversion",
                   expected_container_hash=plans["snapshot"], expected_item_version=plans["rows"][0]["item_version"])
    assert triaged["outcome"] == "committed", triaged
    # Store provenance: no item file appears as a version source of the converted collection.
    inspected = call(root, "record_memory", action="inspect", collection=WORK)
    assert inspected["contract"]["view_mode"] == "summary"
    assert not [entry for entry in call(root, "record_memory", action="query", collection=WORK).get(
        "source_versions", []) if "/Items/" in entry["path"]]
    # A second run finds the collection converted and changes nothing.
    again = convert(root, WORK)
    assert again["outcome"] == "already_converted" and stored_rows(root, CID) != before[CID][0]


def test_conversion_refuses_when_a_rule_scoped_to_an_item_path_would_change_who_sees_the_row(served):
    """Defect: conversion moves a row's policy path to the collection path and so widens access that
    a standing rule on its item file withheld, instead of refusing and naming the scope."""
    root = served
    (item,) = item_files(root, WORK)
    write_scope(root, paths=item.removeprefix("Knowledge Base/"), default_deny=True)
    write_rule(root, ceiling=6)
    with pytest.raises(Exception) as error:
        convert(root, WORK)
    assert error.value.code == "COLLECTION_CONVERSION_DISCLOSURE_CHANGE"
    assert SCOPE_ID in str(error.value)
    assert store(root, "SELECT view_mode FROM collections WHERE collection_id=?", CID) == [("items",)]
    assert item_files(root, WORK)


def test_an_update_to_a_converted_row_files_its_due_state_under_the_collection(served, monkeypatch):
    """Defect: an update to a converted row files its due-state values under the old item path, while an
    append and a full due-state pass file a summary row under its collection, so the row is listed twice."""
    from exomem import due_state

    monkeypatch.setattr(records, "_due_state_carrier", DUE_STATE_CARRIER)
    root = served
    convert(root, WORK)
    with request_scope(OWNER):
        due_state.reconcile(root)  # The projection the write keeps current.
    work = call(root, "record_memory", action="query", collection=WORK)
    updated = call(root, "record_memory", action="update", collection=WORK, item_key=KEY, changes={"count": 7},
                   why="after conversion", expected_container_hash=work["snapshot"],
                   expected_item_version=work["rows"][0]["item_version"])
    assert updated["outcome"] == "committed", updated
    filed = [item["path"] for item in due_state.load(root)["claims"][WORK]["items"] if item["key"] == KEY]
    assert filed == [WORK]


def test_conversion_refuses_when_a_connector_capture_folder_holds_the_items_but_not_the_collection(
        served, tmp_path, monkeypatch):
    """Defect: conversion moves a row's policy path out of a connector's capture folder, so the rule that a
    captured page keeps exactly the membership its path gives stops applying to the connector's writes."""
    root = served
    (item,) = item_files(root, WORK)
    grant_writer(root, tmp_path, monkeypatch, writable_paths=["Records/**"],
                 capture_paths=[str(Path(item).parent)])
    with pytest.raises(collections.CollectionError) as error:
        convert(root, WORK)
    assert error.value.code == "COLLECTION_CONVERSION_DISCLOSURE_CHANGE"
    assert "capture" in str(error.value)
    assert store(root, "SELECT view_mode FROM collections WHERE collection_id=?", CID) == [("items",)]
    assert item_files(root, WORK)


@pytest.mark.parametrize("cut", ["_archive", "_remove"])
def test_a_conversion_cut_after_its_commit_resumes_without_repeating_finished_work(served, monkeypatch, cut):
    """Defect: a crash after the store commit leaves item files that nothing removes, removes a
    file before its archive copy is verified, or a re-run archives or commits a second time."""
    root = served
    files = item_files(root, WORK)
    real = getattr(conversion, cut)

    def crash(*args, **kwargs):
        raise SystemExit("injected crash")

    monkeypatch.setattr(conversion, cut, crash)
    with pytest.raises(SystemExit):
        convert(root, WORK)
    # The store committed; the files are still in place, with or without a verified archive.
    assert store(root, "SELECT view_mode FROM collections WHERE collection_id=?", CID) == [("summary",)]
    assert item_files(root, WORK) == files
    revisions = len(events(root, WORK))
    monkeypatch.setattr(conversion, cut, real)
    if cut == "_remove":
        monkeypatch.setattr(conversion, "_archive", lambda *a, **k: pytest.fail("a verified archive was rebuilt"))
    receipt = convert(root, WORK)
    assert (receipt["outcome"], receipt["phase"]) == ("converted", "complete"), receipt
    assert item_files(root, WORK) == {}
    assert len(events(root, WORK)) == revisions
    listed = json.loads((Path(receipt["archive"]) / "manifest.json").read_text())
    assert {entry["path"]: entry["sha256"] for entry in listed["files"]} == files


def test_an_old_item_path_reads_as_its_store_row_and_a_file_written_there_is_held(served):
    """Defect: after conversion an old item path reads as missing, a link to it breaks, or a file
    written at that path is applied to the row instead of being held."""
    root = served
    (item,) = item_files(root, WORK)
    convert(root, WORK)

    page = call(root, "read_memory", path=item)
    assert page["store_row"]["record_id"] == KEY and page["store_row"]["title"] == "One"
    assert page["read_only"] is True and page["source"] == "collection store"
    # A wikilink names the page without its folder prefix or suffix; the read resolves that spelling too.
    linked = call(root, "read_memory", path=item.removeprefix("Knowledge Base/").removesuffix(".md"))
    assert linked["store_row"] == page["store_row"]
    (entry,) = call(root, "browse_memory", mode="list", path=item)["entries"]
    assert (entry["path"], entry["type"], entry["read_only"]) == (item, "store_row", True)
    assert not (root / item).exists()

    (root / item).write_text("---\ntitle: Replaced outside\ncount: 99\n---\n")
    runtime.notify_view_change(root, root / item)
    _until(lambda: store(root, "SELECT code FROM held_candidates WHERE collection_id=? AND code=?",
                         CID, edit_back.UNBOUND), 30)
    rows = call(root, "record_memory", action="query", collection=WORK)["rows"]
    assert [(row["title"], row["count"]) for row in rows] == [("One", 3)]


def test_an_item_body_reads_the_same_through_the_public_tools_before_and_after_conversion(served):
    """Defect: conversion leaves a row's Markdown body only in the store, where no tool reads it, so the
    body an owner wrote is lost to every reader once the item file is archived."""
    root = served
    (item,) = item_files(root, WORK)
    plans = call(root, "plan_memory", action="query", collection=PLANNING)
    call(root, "plan_memory", action="update", collection=PLANNING, plan_id=PLAN, body="Why this outcome.\n",
         why="a plan body", expected_container_hash=plans["snapshot"],
         expected_item_version=plans["rows"][0]["item_version"])
    _until(lambda: settled(root, OTHER), 30)

    def bodies():
        record = call(root, "record_memory", action="query", collection=WORK, item_key=KEY)
        plan = call(root, "plan_memory", action="query", collection=PLANNING, plan_id=PLAN)
        assert [row["record_id"] for row in record["rows"]] == [KEY]
        assert [row["plan_id"] for row in plan["rows"]] == [PLAN]
        return record["body"], plan["body"], call(root, "read_memory", path=item)["body"]

    before = bodies()
    assert before[:2] == ("Authored body\n", "Why this outcome.\n")
    for path in (WORK, PLANNING):
        convert(root, path)
    assert not (root / item).exists()
    assert bodies() == before


def as_guest(root, tool, **arguments):
    command = next(command for command in commands.product_commands_for("mcp") if command.name == tool)
    with request_scope(RequestPrincipal(audience_id=EXTERNAL, surface="mcp")):
        return writer_lease.invoke_command(command, root, **arguments)


def test_a_guest_reads_an_old_item_path_only_as_far_as_the_row_is_released(served):
    """Defect: the read of a retired item path tells a guest that a withheld row exists, by an answer
    that differs from a missing file's, or withholds the body of a row the guest may read whole."""
    root = served
    (item,) = item_files(root, WORK)
    convert(root, WORK)
    missing = item.replace("Original", "Never-written")
    write_scope(root, paths="Records/Work/**", default_deny=True)
    write_rule(root, ceiling=0)
    answers = []
    for path in (item, missing):
        with pytest.raises(ValueError) as error:
            as_guest(root, "read_memory", path=path)
        answers.append(str(error.value))
    assert answers[0].startswith("NOT_FOUND") and answers[0].replace(item, missing) == answers[1]

    write_rule(root, ceiling=6, extra="# The guest reads the collection whole.\n")
    page = as_guest(root, "read_memory", path=item)
    assert (page["store_row"]["record_id"], page["body"]) == (KEY, "Authored body\n")


def test_conversion_refuses_while_a_view_edit_is_unsettled(served, monkeypatch):
    """Defect: conversion archives and removes an item file whose edit edit-back has not yet applied
    or held, so the owner's edit is lost."""
    root = served
    (item,) = item_files(root, WORK)
    # No census pass may apply the edit first: the next one is an hour away once the due one has run.
    monkeypatch.setattr(edit_back, "FULL_PASS_SECONDS", 3600)
    time.sleep(1)
    edited = root / item
    edited.write_text(edited.read_text().replace("title: One", "title: Edited"))
    refused(lambda: convert(root, WORK), "COLLECTION_CONVERSION_OUTSTANDING")
    assert edited.exists() and "title: Edited" in edited.read_text()


def test_a_converted_collection_keeps_its_child_rows_and_their_declared_columns(env, monkeypatch):  # noqa: F811
    """Defect: conversion drops the record_presentation tables, so after the move to SQLite a child-row
    query or a saved view over a table field refuses, or returns child keys the table never declared."""
    root = legacy_vault(env)
    path = root / OBSERVED
    path.parent.mkdir(parents=True)
    path.write_text(observed_manifest().replace("record_presentation:\n", (
        "views:\n  measured:\n    query:\n      expand_child: measurements\n      columns: [name, value]\n"
        "record_presentation:\n"), 1))
    (path.parent / "Items").mkdir()
    # Each stored child row also carries a "private" key that the table does not declare.
    records.append_record(root, collections.load_manifest(root, path), item=observation(child_count=3),
                          item_key=OBSERVATION, why="seed one observation")
    stop_writer()
    assert migrate(root, "apply") == 0
    service = serve(root, monkeypatch)
    try:
        def answers():
            plain = call(root, "record_memory", action="query", collection=OBSERVED)
            expanded = call(root, "record_memory", action="query", collection=OBSERVED, expand_children=True)
            viewed = call(root, "record_memory", action="query", collection=OBSERVED, view="measured")
            return plain["rows"], expanded["rows"], viewed["rows"]

        _until(lambda: settled(root, OBSERVED_ID), 30)
        before = answers()
        receipt = convert(root, OBSERVED)
        assert (receipt["outcome"], receipt["phase"]) == ("converted", "complete"), receipt
        after = answers()
    finally:
        service.stop()
    assert after == before
    plain, expanded, viewed = after
    assert [len(row["measurements"]) for row in plain] == [3]
    assert [row["child_index"] for row in expanded] == [0, 1, 2]
    assert [sorted(set(row) & {"name", "value", "unit", "private"}) for row in viewed] == [["name", "value"]] * 3
    assert "not projected" not in json.dumps(after)


def test_a_note_that_links_to_an_old_item_path_resolves_it_to_the_store_row(served):
    """Defect: after conversion a note's link to an old item path reads as dangling: the note's write
    warns that the link resolves to no file, the audit reports it, or the item's backlinks lose the note."""
    root = served
    (item,) = item_files(root, WORK)
    convert(root, WORK)
    target = item.removeprefix("Knowledge Base/").removesuffix(".md")
    note = {"content": f"# Follow-up\n\n- [review state] The work in [[{target}]] is done.\n",
            "title": "Follow-up", "slug": "follow-up", "suggestions": False}
    draft = call(root, "remember", validate_only=True, **note)
    saved = call(root, "remember", draft_id=draft["draft_id"], draft_hash=draft["draft_hash"],
                 draft_token=draft["draft_token"], **note)
    assert not [warning for warning in saved["warnings"] if target in warning], saved["warnings"]

    # The note keeps the link in its full form, and following it reads the stored row.
    content = call(root, "read_memory", path=saved["path"], include_raw=True)["content"]
    assert f"[[{item.removesuffix('.md')}]]" in content
    followed = call(root, "read_memory", path=item.removesuffix(".md"))
    assert followed["store_row"]["record_id"] == KEY and followed["read_only"] is True

    inbound = call(root, "connect_memory", operation="inbound-links", target=item)
    assert [link["path"] for link in inbound["inbound"]] == [saved["path"]]
    audit = call(root, "review_memory", mode="audit", categories=["broken_wikilink", "forward_reference"])
    assert [finding for finding in audit["findings"] if finding["path"] == saved["path"]] == []
    assert not (root / item).exists()


def test_a_query_keeps_a_link_to_an_old_item_path_when_the_reader_may_read_that_row(
        env, monkeypatch):  # noqa: F811 - the imported fixture, by name
    """Defect: after conversion a query drops a link field whose value names an old item path, though a
    read of that path answers the stored row; or two rows that link to each other's old paths make
    the link check read rows without end."""
    root = legacy_vault(env, relationships=True)
    assert migrate(root, "apply") == 0
    service = serve(root, monkeypatch)
    try:
        call(root, "record_memory", action="append", collection=WORK, item={"title": "Two", "count": 1},
             why="a second row")
        _until(lambda: settled(root, CID) and len(item_files(root, WORK)) == 2, 30)
        one, two = (path.removeprefix("Knowledge Base/").removesuffix(".md")
                    for path in sorted(item_files(root, WORK), key=lambda path: "Original" not in path))
        # Each row links to the other's item file, so after conversion each link names an old path.
        for title, target in (("One", two), ("Two", one)):
            work = call(root, "record_memory", action="query", collection=WORK)
            row = next(row for row in work["rows"] if row["title"] == title)
            call(root, "record_memory", action="update", collection=WORK, item_key=row["record_id"],
                 changes={"related": f"[[{target}]]"}, why="link the other row",
                 expected_container_hash=work["snapshot"], expected_item_version=row["item_version"])
        _until(lambda: settled(root, CID), 30)
        before = {row["title"]: row.get("related")
                  for row in call(root, "record_memory", action="query", collection=WORK)["rows"]}
        assert before == {"One": f"[[{two}]]", "Two": f"[[{one}]]"}

        convert(root, WORK)
        assert item_files(root, WORK) == {}
        after = {row["title"]: row.get("related")
                 for row in call(root, "record_memory", action="query", collection=WORK)["rows"]}
        assert after == before
        # The stored value is unchanged, and following it reads the other stored row.
        assert call(root, "read_memory", path=two)["store_row"]["title"] == "Two"
    finally:
        service.stop()


def test_the_history_page_of_a_converted_collection_links_only_pages_that_exist(served):
    """Defect: after conversion the generated history page keeps each imported event's link to its
    retired item path, a dead link in the vault."""
    root = served
    page = root / Path(LEDGER).parent / "_history.md"
    convert(root, LEDGER)
    # The page renders again after the conversion, with the events the writer imported for each item.
    _until(lambda: page.exists() and "cutover to summary storage" in page.read_text(), 30)
    links = [line.removeprefix("- [[").removesuffix("]]") for line in page.read_text().splitlines()
             if line.startswith("- [[")]
    assert links and all((root / link).is_file() for link in links), links


def test_conversion_refuses_while_an_edit_to_the_manifest_view_is_unsettled(served, monkeypatch):
    """Defect: conversion republishes the manifest view over an owner's edit that edit-back has not
    yet applied or held, so the edit is lost; a revise refuses in the same state."""
    root = served
    monkeypatch.setattr(edit_back, "FULL_PASS_SECONDS", 3600)
    time.sleep(1)
    manifest = root / WORK
    edited = manifest.read_text() + "\nA note the owner added outside the store.\n"
    manifest.write_text(edited)
    refused(lambda: convert(root, WORK), "COLLECTION_CONVERSION_OUTSTANDING")
    assert store(root, "SELECT view_mode FROM collections WHERE collection_id=?", CID) == [("items",)]
    assert manifest.read_text() == edited and item_files(root, WORK)


@pytest.mark.parametrize("change", ["changed", "missing"])
def test_a_resumed_conversion_names_each_item_file_it_did_not_remove(served, monkeypatch, change):
    """Defect: an item file edited or deleted between the conversion's commit and its archive is left
    out of the receipt, so the owner reads every file as archived and removed when one stays in the
    vault or never reached the archive."""
    root = served
    (item,) = item_files(root, WORK)
    real = conversion._archive

    def crash(*args, **kwargs):
        raise SystemExit("injected crash")

    monkeypatch.setattr(conversion, "_archive", crash)
    with pytest.raises(SystemExit):
        convert(root, WORK)
    monkeypatch.setattr(conversion, "_archive", real)
    edited = root / item
    if change == "changed":
        edited.write_text(edited.read_text().replace("title: One", "title: Edited"))
    else:
        edited.unlink()
    receipt = convert(root, WORK)
    assert (receipt["phase"], receipt["removed"], receipt["not_removed"]) == (
        "complete", 0, [{"path": item, "reason": change}]), receipt
    assert edited.exists() == (change == "changed")
