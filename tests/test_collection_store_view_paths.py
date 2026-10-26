"""Title-bearing collection view paths: the file tools refuse them, and no surface leaks a withheld one.

Each surface test observes one path as the same restricted caller twice, before the
secret row exists and once the row is withheld, and requires the same answer. The
owner's answer shows that the surface does expose the view to a reader who may see it.
"""

import json
import shutil
from dataclasses import replace

import pytest
from test_collection_store_writer import CID, KEY, OTHER, manifest_path, manifest_text
from test_collection_store_writer import store as store
from test_governance_egress import _external, _through_dispatcher, write_rule, write_scope

from exomem import reserved_paths
from exomem import structured_collections as collections
from exomem.collection_store import authority
from exomem.collection_store.preview import preview_store
from exomem.governance import egress
from exomem.governance.principal import owner_principal, request_scope

WORK = "Knowledge Base/Records/Work"
ITEMS = f"{WORK}/Items"
OPEN = f"{ITEMS}/Open plan.md"
SECRET = f"{ITEMS}/Acquire Northwind.md"
SECRET_ITEM = {"title": "Acquire Northwind"}
SECRET_HOLD = {**SECRET_ITEM, "tags": ["secret"], "count": "many"}
TARGET = "Knowledge Base/Notes/target.md"


@pytest.fixture
def routed(store):
    """A store collection that names its views by title, and a marker routing it."""
    text = manifest_text().replace(
        "    count:", "    tags: {type: array, items: {type: string}}\n    count:")
    text = text.replace("storage:\n", "item_filename:\n  version: 1\n  fields: [title]\nstorage:\n")
    store.create_collection(manifest_path(), text, why="create")
    store.append_record(CID, item={"title": "Open plan"}, item_key=KEY, why="capture")
    store.reconcile_views()
    sid = store.connection.execute("SELECT value FROM store_meta WHERE key='store_id'").fetchone()[0]
    marker = authority.marker_path(store.root)
    marker.parent.mkdir(parents=True)
    marker.write_text(json.dumps({
        "version": 2, "mode": "store", "default_authority": "file", "store_id": sid,
        "authority_epoch": 1,
        "collections": [{"collection_id": CID, "manifest_path": manifest_path(),
                         "authority": "store", "store_id": sid, "semantic_profile": "records",
                         "source_path": ITEMS, "layout": "markdown-items"}],
        "collection_store_fence": {"capability": "collections-store-v1", "generation": 1},
    }))
    return store


@pytest.fixture
def vault(routed):
    """The routed collection under a policy.

    The policy withholds the secret row from the external audience by its reference,
    which the view's bytes do not carry, so only the item's own authorization withholds
    it. A hold is its own subject: it keeps its candidate's tags but not the reference.
    """
    store = routed
    write_scope(store.root, paths="Unrelated/**")
    scope = store.root / "Knowledge Base/_Governance/scopes/patterns.yaml"
    scope.write_text(scope.read_text() + f"refs: [exomem://record/{CID}/{OTHER}]\ntags: [secret]\n")
    write_rule(store.root, ceiling=0)
    target = store.root / TARGET
    target.parent.mkdir(parents=True)
    target.write_text("---\ntype: insight\nstatus: active\n---\n\nTarget.\n")
    store.reconcile_views()
    return store


def _call(vault, name, **arguments):
    """One answer through the dispatcher; a refusal is the text the caller receives."""
    try:
        return _through_dispatcher(vault.root, name, **arguments)
    except ValueError as error:
        return str(error)


def _as(vault, who, observe):
    with preview_store(vault.root, vault.handle), request_scope(who):
        return observe()


def _add_secret(vault):
    vault.append_record(CID, item=SECRET_ITEM, item_key=OTHER, body="See [[Notes/target]].\n", why="capture")
    vault.reconcile_views()


def _files(vault):
    return sorted(path.relative_to(vault.root).as_posix() for path in (vault.root / WORK).rglob("*"))


def test_file_tools_answer_a_withheld_view_as_missing_and_refuse_a_released_one(vault):
    def observe():
        return [_call(vault, "delete", path=SECRET, confirm=True),
                _call(vault, "move_file", old_path=SECRET, new_path="Knowledge Base/Notes/moved.md")]

    absent = _as(vault, _external(), observe)
    _add_secret(vault)
    views = _files(vault)
    assert _as(vault, _external(), observe) == absent == [f"NOT_FOUND: path does not exist: {SECRET}"] * 2
    released = _as(vault, _external(), lambda: [
        _call(vault, "delete", path=OPEN, confirm=True),
        _call(vault, "move_file", old_path=OPEN, new_path="Knowledge Base/Notes/moved.md")])
    assert [str(answer).split(":")[0] for answer in released] == ["COLLECTION_VIEW_PATH"] * 2
    # The marker names the collection's profile, so the remediation names its one tool.
    assert "record_memory" in released[0] and "plan_memory" not in released[0]
    assert _files(vault) == views


def test_the_owner_file_tools_refuse_view_paths_from_canonical_ownership(vault):
    """An explicitly bound private canonical writer protects both endpoints and their ancestors."""
    plain, trashed = "Knowledge Base/Notes/plain.md", "Knowledge Base/Notes/trashed.md"
    for note in (plain, trashed):
        (vault.root / note).write_text("Plain note.\n")
    with preview_store(vault.root, vault.handle), request_scope(owner_principal()):
        _call(vault, "delete", path=trashed, confirm=True)
        in_trash = next((vault.root / "Knowledge Base/_trash").rglob("*trashed.md")).relative_to(vault.root)
        views = _files(vault)
        refusals = [
            _call(vault, "delete", path=OPEN, confirm=True),
            _call(vault, "delete", path=WORK, confirm=True, recursive=True),
            _call(vault, "move_file", old_path=plain, new_path=f"{ITEMS}/plain.md"),
            _call(vault, "recover_from_trash", trash_path=in_trash.as_posix(), restore_path=f"{ITEMS}/trashed.md"),
        ]
    # Bound previews retain the existing whole-folder floor; production ownership is covered by the service workflow.
    assert [str(answer).split(":")[0] for answer in refusals] == [
        "COLLECTION_VIEW_PATH", "AUDIENCE_RESTRICTED", "COLLECTION_VIEW_PATH", "COLLECTION_VIEW_PATH"]
    assert _files(vault) == views


def test_restoring_a_folder_where_a_collection_lost_its_views_refuses_them(vault):
    """The views went missing outside Exomem; the restore path holds none, but its children would."""
    for name in ("Items/Open plan.md", "notes.md"):
        (vault.root / "Knowledge Base/Notes/Old" / name).parent.mkdir(parents=True, exist_ok=True)
        (vault.root / "Knowledge Base/Notes/Old" / name).write_text("Stale copy.\n")
    with preview_store(vault.root, vault.handle), request_scope(owner_principal()):
        from exomem.delete_directory import delete_directory

        delete_directory(vault.root, path="Knowledge Base/Notes/Old", confirm=True, recursive=True)
        in_trash = next(path for path in (vault.root / "Knowledge Base/_trash").rglob("*Old") if path.is_dir())
        shutil.rmtree(vault.root / WORK)
        refusal = _call(vault, "recover_from_trash", trash_path=in_trash.relative_to(vault.root).as_posix(),
                        restore_path=WORK)
    assert str(refusal).startswith("COLLECTION_VIEW_PATH:")
    assert not (vault.root / WORK).exists()


def test_another_casing_of_the_knowledge_base_folder_is_refused_on_a_case_folding_volume(vault, monkeypatch):
    """`move_file` rebuilds a protected destination from the caller's own spelling of the folder."""
    monkeypatch.setenv("EXOMEM_CASEFOLD_PATHS", "1")
    with preview_store(vault.root, vault.handle), pytest.raises(ValueError, match="COLLECTION_VIEW_PATH"):
        authority.refuse_view_paths(vault.root, (OPEN.replace("Knowledge Base", "knowledge base"),), ValueError)


def test_file_tools_keep_file_collections_and_open_no_store_without_a_marker(tmp_path, monkeypatch):
    from exomem import commands, records
    from exomem.collection_store import connection

    for name in ("load_store_libraries", "open_reader", "open_writer"):
        monkeypatch.setattr(connection, name, lambda *a, **kw: pytest.fail("a file tool opened the store"))
    root = tmp_path / "vault"
    (root / "Knowledge Base").mkdir(parents=True)
    (root / "Knowledge Base/log.md").write_text("# Log\n")
    records.create_collection(root, manifest_path(), manifest_text(), why="create")
    item = records.append_record(root, CID, item={"title": "Open plan"}, item_key=KEY, why="capture")
    page = item["affected_paths"][0]
    moved = "Knowledge Base/Notes/moved.md"
    with request_scope(owner_principal()):
        commands.op_move_file(root, old_path=page, new_path=moved, update_wikilinks=False)
        trashed = commands.op_delete(root, path=moved, confirm=True)
        commands.op_recover_from_trash(root, trash_path=trashed["trash_path"], restore_path=page)
    assert (root / page).is_file() and not (root / moved).exists()


def test_listing_and_counts_omit_a_withheld_view(vault):
    def observe():
        return (_call(vault, "list_directory", path=ITEMS),
                _call(vault, "list_directory", path="Knowledge Base/Records", recursive=True),
                _call(vault, "overview", path="Knowledge Base/Records")["totals"])

    absent = _as(vault, _external(), observe)
    _add_secret(vault)
    assert _as(vault, _external(), observe) == absent
    assert SECRET in str(_as(vault, owner_principal(), observe))


def test_inbound_links_omit_a_withheld_view(vault):
    def observe():
        return _call(vault, "list_inbound_links", target=TARGET)

    absent = _as(vault, _external(), observe)
    _add_secret(vault)
    assert _as(vault, _external(), observe) == absent
    assert SECRET in str(_as(vault, owner_principal(), observe))


def test_a_withheld_view_title_does_not_resolve_a_writers_link(vault):
    def write(name):
        result = _call(vault, "create_file", path=f"Knowledge Base/Inbox/{name}.md", content="See [[Acquire Northwind]].\n")
        return (vault.root / f"Knowledge Base/Inbox/{name}.md").read_text(), result["warnings_count"]

    absent = _as(vault, _external(), lambda: write("before"))
    _add_secret(vault)
    assert _as(vault, _external(), lambda: write("after")) == absent
    assert SECRET.removesuffix(".md") in _as(vault, owner_principal(), lambda: write("owner"))[0]


def test_reading_a_withheld_view_refuses_as_a_missing_path(vault):
    def observe():
        return [_call(vault, "get", path=SECRET), _call(vault, "get", path=SECRET, include_raw=True)]

    absent = _as(vault, _external(), observe)
    _add_secret(vault)
    assert _as(vault, _external(), observe) == absent
    assert SECRET in str(_as(vault, owner_principal(), observe))


def test_a_withheld_held_view_is_absent_from_its_collection_listing(vault):
    """Reading it is covered in `test_projected_page_cannot_drop_canonical_restricted_tags`."""
    def observe():
        return _call(vault, "list_directory", path=WORK, recursive=True)

    absent = _as(vault, _external(), observe)
    with pytest.raises(collections.CollectionError) as refused:
        vault.append_record(CID, item=SECRET_HOLD, item_key=OTHER, why="capture")
    assert _as(vault, _external(), observe) == absent
    assert refused.value.details["held"]["path"] in str(_as(vault, owner_principal(), observe))


def test_a_hold_that_targets_a_withheld_item_is_absent(vault):
    """The hold carries no tag: only the item it would change is withheld, by its reference."""
    _add_secret(vault)

    def listing():
        return _call(vault, "list_directory", path=WORK, recursive=True)

    absent = _as(vault, _external(), listing)
    with pytest.raises(collections.CollectionError) as refused:
        vault.append_record(CID, item={**SECRET_ITEM, "count": "many"}, item_key=OTHER, why="correct")
    held = refused.value.details["held"]

    def read():
        return _call(vault, "get", path=held["path"])

    withheld = _as(vault, _external(), lambda: [listing(), read()])
    assert "Acquire Northwind" in str(_as(vault, owner_principal(), read))
    # Discarding the hold leaves its folder behind, so the listing compares with the hold never made.
    vault.discard_held(CID, held=held["held_id"], why="discard")
    assert withheld == [absent, _as(vault, _external(), read)]


def test_a_held_view_edit_of_a_withheld_item_is_absent(vault, tmp_path):
    """A view correction holds the edited bytes, which carry the item's title, at a path of its own."""
    _add_secret(vault)

    def observe():
        return [_call(vault, "list_directory", path=WORK, recursive=True),
                _call(vault, "record_memory", action="inspect")["collections"][0]["held"]]

    absent = _as(vault, _external(), observe)
    page = vault.root / SECRET
    page.write_text(page.read_text() + "\nA local edit.\n")
    inspected = _as(vault, owner_principal(), lambda: _call(vault, "record_memory", action="inspect", collection=CID))
    version = next(entry["hash"] for entry in inspected["source_versions"] if entry["path"] == SECRET)
    vault.update_record(CID, item_key=OTHER, changes={"count": 3}, why="count",
                        expected_container_hash=inspected["lifecycle_guards"]["expected_container_hash"],
                        expected_item_version=version)
    vault.reconcile_views()
    assert _as(vault, _external(), observe) == absent
    owner = _as(vault, owner_principal(), lambda: _call(vault, "list_directory", path=f"{WORK}/Held"))
    (held_path,) = [entry["path"] for entry in owner["entries"]]
    assert "A local edit" in str(_as(vault, owner_principal(), lambda: _call(vault, "get", path=held_path)))
    withheld = _as(vault, _external(), lambda: _call(vault, "get", path=held_path))
    (vault.root / held_path).rename(tmp_path / "aside.md")
    assert _as(vault, _external(), lambda: _call(vault, "get", path=held_path)) == withheld


@pytest.mark.parametrize("candidate", ["[1]", "not json", None])
def test_a_hold_whose_item_cannot_be_read_is_withheld_from_all_but_the_owner(vault, candidate):
    """Such a hold could follow any item; the collection must still list, read and count."""
    with pytest.raises(collections.CollectionError) as refused:
        vault.append_record(CID, item={"title": "Open plan", "count": "many"}, item_key=KEY, why="correct")
    vault.connection.execute("UPDATE held_candidates SET candidate_json=?", (candidate,))

    def observe():
        answer = _call(vault, "get", path=refused.value.details["held"]["path"])
        return isinstance(answer, dict), _call(vault, "record_memory", action="inspect")["collections"][0]["held"]

    assert _as(vault, _external(), observe) == (False, 0)
    assert _as(vault, owner_principal(), observe) == (True, 1)


def test_a_warm_release_cache_withholds_a_hold_once_its_item_is_withheld(vault):
    """The cache decides changed rows again; a hold of a changed item must be decided with it."""
    with pytest.raises(collections.CollectionError):
        vault.append_record(CID, item={"title": "Open plan", "count": "many"}, item_key=KEY, why="correct")

    def held():
        return _call(vault, "record_memory", action="inspect")["collections"][0]["held"]

    assert _as(vault, _external(), held) == 1
    inspected = _as(vault, owner_principal(), lambda: _call(vault, "record_memory", action="inspect", collection=CID))
    version = next(entry["hash"] for entry in inspected["source_versions"] if entry["path"] == OPEN)
    vault.update_record(CID, item_key=KEY, changes={"tags": ["secret"]}, why="restrict",
                        expected_container_hash=inspected["lifecycle_guards"]["expected_container_hash"],
                        expected_item_version=version)
    assert _as(vault, _external(), held) == 0
    assert _as(vault, owner_principal(), held) == 1


def test_inventory_counts_omit_a_withheld_row_and_its_hold(vault):
    def observe():
        return _call(vault, "record_memory", action="inspect")["collections"]

    absent = _as(vault, _external(), observe)
    _add_secret(vault)
    with pytest.raises(collections.CollectionError):
        vault.append_record(CID, item={**SECRET_HOLD, "title": "Hold Northwind"},
                            item_key="33333333-3333-4333-8333-333333333333", why="capture")
    assert _as(vault, _external(), observe) == absent
    owner = _as(vault, owner_principal(), observe)
    assert (owner[0]["committed"], owner[0]["held"]) == (2, 1)


def test_the_records_inventory_sweeps_every_file_but_the_store_views_for_trackers(routed, monkeypatch):
    beside = f"{WORK}/Log.md"
    elsewhere = "Knowledge Base/Records/Legacy/Log.md"
    for path in (beside, elsewhere):
        (routed.root / path).parent.mkdir(parents=True, exist_ok=True)
        (routed.root / path).write_text("---\ntype: tracker\n---\n\n| Date | Entry |\n|---|---|\n")
    inspected = []
    inspect = collections.inspect_legacy_tracker

    def counted(root, path, **kwargs):
        inspected.append(path.relative_to(root).as_posix())
        return inspect(root, path, **kwargs)

    monkeypatch.setattr(collections, "inspect_legacy_tracker", counted)
    found = _as(routed, owner_principal(), lambda: _call(routed, "record_memory", action="inspect"))
    assert {row["path"] for row in found["legacy_trackers"]} == {beside, elsewhere}
    views = {path for (path,) in routed.connection.execute("SELECT view_path FROM items")}
    assert views == {OPEN} and not views & set(inspected)


def test_a_sync_conflict_copy_of_a_withheld_view_is_absent(vault):
    copy = f"{ITEMS}/Acquire Northwind.sync-conflict-20261016-101500-ABCDEFG.md"

    def observe():
        return [_call(vault, "list_directory", path=ITEMS), _call(vault, "get", path=copy)]

    absent = _as(vault, _external(), observe)
    _add_secret(vault)
    # The copy carries the row's own stamp and bytes, which is how a sync client makes one.
    (vault.root / copy).write_bytes((vault.root / SECRET).read_bytes())
    assert _as(vault, _external(), observe) == absent


def test_a_casefold_folder_spelling_cannot_bypass_the_aggregate_view_check(vault, monkeypatch):
    """An owner aggregate must refuse an edited view even when the volume changes folder casing."""
    monkeypatch.setenv("EXOMEM_CASEFOLD_PATHS", "1")
    page = vault.root / OPEN
    page.write_text(page.read_text().replace("Open plan", "Edited outside the store"))
    entries = reserved_paths.list_generic_tree(vault.root, ".")
    recased = tuple(
        replace(entry, relative_path=entry.relative_path.replace("Knowledge Base/", "knowledge base/", 1))
        for entry in entries
    )
    monkeypatch.setattr(reserved_paths, "list_generic_tree", lambda *_args, **_kwargs: recased)

    assert _as(vault, owner_principal(), lambda: egress.owner_only_aggregate(vault.root)) == {
        "available": False, "reason": "audience_restricted",
    }


@pytest.mark.parametrize("dry_run", [True, False])
def test_maintenance_never_reports_or_repairs_a_restricted_vault_aggregate(vault, dry_run):
    """A deleted hidden view must not change maintenance findings, counts or status."""
    from exomem import commands, memory_refs, writer_lease

    command = next(command for command in commands.PRODUCT_COMMANDS if command.name == "maintain_memory")

    def report():
        return writer_lease.invoke_command(command, vault.root, mode="reconcile", dry_run=dry_run)

    _add_secret(vault)
    memory_refs.ReferenceIndex(vault.root).rebuild_all()
    before = _as(vault, _external(), report)
    (vault.root / SECRET).unlink()
    drift = memory_refs.drift(vault.root)
    assert any(finding["path"] == SECRET for finding in drift)
    assert _as(vault, _external(), report) == before == {
        "available": False, "reason": "audience_restricted",
    }
    # A refused write-mode maintenance call cannot silently repair the hidden input.
    assert memory_refs.drift(vault.root) == drift
    owner = _as(vault, owner_principal(), lambda: writer_lease.invoke_command(
        command, vault.root, mode="reconcile", dry_run=True))
    assert SECRET in json.dumps(owner["remaining_drift"])
    assert owner["references_refreshed"] == len(drift)
