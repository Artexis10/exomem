"""Corrections to the dark writer's existing built-in contracts."""

import json
import time
import uuid

import pytest

from exomem import mutation_terminal, planning, record_formats, structured_collections as collections
from exomem.cli_ops import OpError
from exomem.collection_store import tokens
from test_collection_store_wire import paired as paired, paired_create, same_wire
from test_collection_store_writer import CID, KEY, manifest_path, manifest_text, store as store


def test_planning_defaulted_natural_key_matches_file_identity(paired):
    invoke, _, handle = paired
    text = manifest_text("planning").replace("natural_key: [title]", "natural_key: [title, kind]")
    for mode in (0, 1):
        invoke(mode, "planning", "create", manifest_path=manifest_path("planning"),
               manifest_text=text, why="create")
    receipts = [invoke(mode, "planning", "add", collection=CID,
                       item={"title": "One"}, why="capture") for mode in (0, 1)]
    assert receipts[0]["plan_id"] == receipts[1]["plan_id"]
    same_wire(*receipts)
    values = json.loads(handle.connection.execute("SELECT values_json FROM items").fetchone()[0])
    assert values["kind"] == "work-item"


@pytest.mark.parametrize("size", [100, 500])
def test_planning_revision_reads_and_normalizes_graph_once(store, monkeypatch, size):
    text = manifest_text("planning")
    made = store.create_collection(manifest_path("planning"), text, why="create", scaffold=False)
    encoded_values = set()
    with store.handle.transaction() as conn:
        for index in range(size):
            key = str(uuid.UUID(int=index + 1))
            values = planning.normalize_item({"title": f"Item {index}"}, vault_root=None)
            encoded = json.dumps(values)
            encoded_values.add(encoded)
            conn.execute(
                "INSERT INTO items(collection_id,item_key,natural_key,row_version,schema_version,"
                "values_json,body,payload_hash,view_path,created_txn,updated_txn,governance_json) "
                "VALUES(?,?,NULL,1,1,?,'',?,?,1,1,?)",
                (CID, key, encoded, tokens.payload_hash(1, key, values, ""),
                 f"Knowledge Base/Planning/Work/Items/{key}.md",
                 '{"classes":[],"projects":[],"tags":[]}'),
            )
    counts = {"decoded": 0, "normalized": 0, "hierarchy": 0}
    loads, normalize, hierarchy = json.loads, planning.normalize_item, planning.validate_hierarchy

    def counted_loads(value, *args, **kwargs):
        if isinstance(value, str) and value in encoded_values:
            counts["decoded"] += 1
        return loads(value, *args, **kwargs)

    def counted_normalize(*args, **kwargs):
        counts["normalized"] += 1
        return normalize(*args, **kwargs)

    def counted_hierarchy(*args, **kwargs):
        counts["hierarchy"] += 1
        return hierarchy(*args, **kwargs)

    monkeypatch.setattr(json, "loads", counted_loads)
    monkeypatch.setattr(planning, "normalize_item", counted_normalize)
    monkeypatch.setattr(planning, "validate_hierarchy", counted_hierarchy)
    statements = []
    store.connection.set_trace_callback(statements.append)
    started = time.monotonic()
    try:
        store.revise_collection(CID, manifest_text=text.replace("title: Work", "title: Revised"),
                                expected_manifest_hash=tokens.manifest_hash(text),
                                expected_container_hash=made["after_container_hash"], why="revise")
    finally:
        store.connection.set_trace_callback(None)
    reads = sum(sql.startswith("SELECT item_key, values_json FROM items WHERE collection_id")
                for sql in statements)
    print(f"rows={size} reads={reads} counts={counts} elapsed={time.monotonic() - started:.3f}s")
    assert (reads, counts) == (1, {"decoded": size, "normalized": size, "hierarchy": 1})


@pytest.mark.parametrize("profile", ["records", "planning"])
def test_invalid_saved_view_revision_refuses_without_store_changes(paired, profile):
    invoke, _, handle = paired
    paired_create(paired, profile)
    inspections = [invoke(mode, profile, "inspect", collection=CID) for mode in (0, 1)]
    proposed = manifest_text(profile).removesuffix("---\n") + (
        "views:\n  invalid:\n    query:\n      filters: [{column: missing, op: eq, value: 1}]\n---\n"
    )
    before = tuple(handle.connection.iterdump())
    for mode in (0, 1):
        with pytest.raises(OpError) as caught:
            invoke(mode, profile, "revise", collection=CID, manifest_text=proposed,
                   why="revise", **inspections[mode]["lifecycle_guards"])
        assert caught.value.code == "INVALID_SAVED_VIEW"
    assert tuple(handle.connection.iterdump()) == before


@pytest.mark.parametrize("profile", ["records", "planning"])
def test_delimiters_in_quoted_yaml_and_comments_create_and_revise(paired, profile):
    invoke, _, _ = paired
    text = manifest_text(profile).replace("title: Work", 'title: "Work --- Phase" # --- comment')
    receipts = [invoke(mode, profile, "create", manifest_path=manifest_path(profile),
                       manifest_text=text, why="create") for mode in (0, 1)]
    same_wire(*receipts)
    receipts = []
    for mode in (0, 1):
        guards = invoke(mode, profile, "inspect", collection=CID)["lifecycle_guards"]
        receipts.append(invoke(mode, profile, "revise", collection=CID,
                               manifest_text=text.replace("Work --- Phase", "Revised --- Phase"),
                               why="revise", **guards))
    same_wire(*receipts)


@pytest.mark.parametrize("profile", ["records", "planning"])
def test_existing_selector_spellings_resolve_without_canonical_views(paired, monkeypatch, profile):
    invoke, roots, _ = paired
    paired_create(paired, profile)
    path = manifest_path(profile)
    for selector in (f"exomem://memory/{CID}", CID.upper(), f"./{path}", f"  {path}  ", "absolute"):
        args = dict(collection=str(roots[0] / path) if selector == "absolute" else selector)
        file_inspection = invoke(0, profile, "inspect", **args)
        with monkeypatch.context() as guarded:
            guarded.setattr(collections, "load_manifest", lambda *a, **k: pytest.fail("file manifest read"))
            args["collection"] = str(roots[1] / path) if selector == "absolute" else selector
            assert invoke(1, profile, "inspect", **args)["contract"] == file_inspection["contract"]
    for mode in (0, 1):
        for selector in (str(uuid.UUID(int=1)), path.replace("Work/", "Missing/")):
            with pytest.raises(OpError) as caught:
                invoke(mode, profile, "inspect", collection=selector)
            assert caught.value.code == "COLLECTION_NOT_FOUND"


def test_planning_body_only_update_preserves_values(paired):
    invoke, roots, handle = paired
    paired_create(paired, "planning")
    first = [invoke(mode, "planning", "add", collection=CID, plan_id=KEY,
                    item={"title": "One"}, body="Old body\n", why="capture") for mode in (0, 1)]
    old_values = handle.connection.execute("SELECT values_json FROM items").fetchone()[0]
    updated = [invoke(mode, "planning", "update", collection=CID, plan_id=KEY,
                      body="New body\n", expected_container_hash=first[mode]["after_container_hash"],
                      expected_item_version=first[mode]["after_item_hash"], why="correct") for mode in (0, 1)]
    same_wire(*updated)
    assert handle.connection.execute("SELECT body, values_json FROM items").fetchone() == ("New body\n", old_values)
    manifest = collections.load_manifest(roots[0], manifest_path("planning"))
    body = record_formats.load_adapter(roots[0], manifest).read().records[0].body
    assert record_formats.remove_item_presentation(body).strip() == "New body"


@pytest.mark.parametrize("recipe", [False, True])
@pytest.mark.parametrize("changes", [{}, {"title": "Two"}])
def test_refresh_requires_presentation_recipe(paired, recipe, changes):
    invoke, _, handle = paired
    text = manifest_text()
    if recipe:
        text = text.removesuffix("---\n") + (
            "item_presentation:\n  version: 1\n  title: title\n  summary: [count]\n---\n"
        )
    for mode in (0, 1):
        invoke(mode, "records", "create", manifest_path=manifest_path(), manifest_text=text, why="create")
    first = [invoke(mode, "records", "append", collection=CID, item_key=KEY,
                    item={"title": "One"}, why="capture") for mode in (0, 1)]
    before = tuple(handle.connection.iterdump())
    receipts = []
    for mode in (0, 1):
        args = dict(collection=CID, item_key=KEY, changes=changes, refresh_presentation=True,
                    expected_container_hash=first[mode]["after_container_hash"],
                    expected_item_version=first[mode]["after_item_hash"], why="refresh")
        if recipe and changes:
            receipts.append(invoke(mode, "records", "update", **args))
        else:
            with pytest.raises(OpError) as caught:
                invoke(mode, "records", "update", **args)
            assert caught.value.code == ("NOOP_RECORD_PRESENTATION" if recipe else "INVALID_RECORD_PRESENTATION")
    if recipe and changes:
        same_wire(*receipts)
    else:
        assert tuple(handle.connection.iterdump()) == before


@pytest.mark.parametrize("body", [False, 0, [], {}])
def test_falsey_nontext_append_body_refuses_unchanged(paired, body):
    invoke, _, handle = paired
    paired_create(paired, "records")
    before = tuple(handle.connection.iterdump())
    for mode in (0, 1):
        with pytest.raises(OpError) as caught:
            invoke(mode, "records", "append", collection=CID, item_key=KEY,
                   item={"title": "One"}, body=body, why="capture")
        assert caught.value.code == "INVALID_RECORD_BODY"
    assert tuple(handle.connection.iterdump()) == before


@pytest.mark.parametrize("body", [False, 0, [], {}])
def test_held_resume_validates_falsey_nontext_body(store, body):
    store.create_collection(manifest_path(), manifest_text(), why="create")
    with pytest.raises(collections.CollectionError) as caught:
        store.append_record(CID, item={"title": "One", "count": "bad"}, why="capture")
    held = caught.value.details["held"]["held_id"]
    with store.handle.transaction() as conn:
        candidate = json.loads(conn.execute("SELECT candidate_json FROM held_candidates").fetchone()[0])
        candidate["body"] = body
        conn.execute("UPDATE held_candidates SET candidate_json = ?", (json.dumps(candidate),))
    before = tuple(store.connection.iterdump())
    with pytest.raises(collections.CollectionError, match="INVALID_RECORD_BODY"):
        store.append_record(CID, held=held, item={"count": 1}, why="resume")
    assert tuple(store.connection.iterdump()) == before


@pytest.mark.parametrize("body", [False, 0, [], {}])
def test_held_resume_rejects_supplied_falsey_nontext_body(paired, body):
    invoke, _, handle = paired
    paired_create(paired, "records")
    references = []
    for mode in (0, 1):
        with pytest.raises(OpError) as caught:
            invoke(mode, "records", "append", collection=CID, item_key=KEY,
                   item={"title": "One", "count": "bad"}, body="Held body\n", why="capture")
        references.append(caught.value.details["held"]["held_id"])
    before = tuple(handle.connection.iterdump())
    for mode in (0, 1):
        with pytest.raises(OpError) as caught:
            invoke(mode, "records", "append", collection=CID, held=references[mode],
                   item={"count": 1}, body=body, why="resume")
        assert caught.value.code == "INVALID_RECORD_BODY"
    assert tuple(handle.connection.iterdump()) == before


@pytest.mark.parametrize("override", [False, 0, [], "", True, 1, ["count"], "count"])
@pytest.mark.parametrize("action", ["append", "update"])
def test_held_resume_rejects_nonobject_overrides_after_schema_revision(store, action, override):
    store.create_collection(manifest_path(), manifest_text(), why="create")
    args = dict(item={"title": "One", "count": "now-valid"}, item_key=KEY)
    if action == "update":
        first = store.append_record(CID, item={"title": "One"}, item_key=KEY, why="capture")
        args = dict(changes={"count": "now-valid"}, item_key=KEY,
                    expected_container_hash=first["after_container_hash"],
                    expected_item_version=first["after_item_hash"])
    mutate = store.append_record if action == "append" else store.update_record
    with pytest.raises(collections.CollectionError) as caught:
        mutate(CID, **args, why="hold")
    held = caught.value.details["held"]["held_id"]
    guards = store.inspect_collection(CID)["lifecycle_guards"]
    store.revise_collection(CID, manifest_text=manifest_text().replace(
        "count: {type: integer}", "count: {type: string}"), why="revise", **guards)
    resume = dict(held=held, why="resume")
    if action == "append":
        resume["item"] = override
    else:
        row = store._item(CID, KEY)
        resume.update(item_key=KEY, changes=override,
                      expected_container_hash=store.inspect_collection(CID)["snapshot"],
                      expected_item_version=store._version(row))
    before = tuple(store.connection.iterdump())
    with pytest.raises(collections.CollectionError) as caught:
        mutate(CID, **resume)
    assert caught.value.code == ("INVALID_ITEM" if action == "append" else "INVALID_RECORD_CHANGES")
    assert tuple(store.connection.iterdump()) == before
    assert not store.connection.in_transaction


@pytest.mark.parametrize("override", [None, {}, {"title": "Two"}, {"count": None}])
@pytest.mark.parametrize("action", ["append", "update"])
def test_held_resume_preserves_mapping_and_omitted_overrides(store, action, override):
    store.create_collection(manifest_path(), manifest_text(), why="create")
    args = dict(item={"title": "One", "count": "now-valid"}, item_key=KEY)
    if action == "update":
        first = store.append_record(CID, item={"title": "One"}, item_key=KEY, why="capture")
        args = dict(changes={"title": "One", "count": "now-valid"}, item_key=KEY,
                    expected_container_hash=first["after_container_hash"],
                    expected_item_version=first["after_item_hash"])
    mutate = store.append_record if action == "append" else store.update_record
    with pytest.raises(collections.CollectionError) as caught:
        mutate(CID, **args, why="hold")
    held = caught.value.details["held"]["held_id"]
    guards = store.inspect_collection(CID)["lifecycle_guards"]
    store.revise_collection(CID, manifest_text=manifest_text().replace(
        "count: {type: integer}", "count: {type: string}"), why="revise", **guards)
    resume = dict(held=held, why="resume")
    if override is not None:
        resume["item" if action == "append" else "changes"] = override
    if action == "update":
        row = store._item(CID, KEY)
        resume.update(item_key=KEY, expected_item_version=store._version(row),
                      expected_container_hash=store.inspect_collection(CID)["snapshot"])
    result = mutate(CID, **resume)
    assert result["outcome"] == "committed"
    values = json.loads(store.connection.execute("SELECT values_json FROM items").fetchone()[0])
    assert values["title"] == ("Two" if override == {"title": "Two"} else "One")
    if override == {"count": None}:
        assert "count" not in values
    else:
        assert values["count"] == "now-valid"
    assert store.connection.execute("SELECT COUNT(*) FROM held_candidates").fetchone()[0] == 0


@pytest.mark.parametrize("profile", ["records", "planning"])
@pytest.mark.parametrize("shape", ["directory", "missing-directory", "missing-item", "foreign-missing-item"])
def test_missing_selector_shape_preserves_facade_refusals(paired, monkeypatch, profile, shape):
    invoke, roots, handle = paired
    path = manifest_path(profile)
    for mode in (0, 1):
        invoke(mode, profile, "create", manifest_path=path,
               manifest_text=manifest_text(profile), why="create")
    selector = {
        "directory": path.removesuffix("/_collection.md"),
        "missing-directory": path.replace("Work/_collection.md", "Missing"),
        "missing-item": path.replace("_collection.md", "Items/missing.md"),
        "foreign-missing-item": manifest_path("planning" if profile == "records" else "records").replace(
            "_collection.md", "Items/missing.md"),
    }[shape]
    expected = "COLLECTION_NOT_FOUND" if profile == "records" and shape.endswith("missing-item") else "INVALID_COLLECTION_PATH"
    before = tuple(handle.connection.iterdump())
    with pytest.raises(OpError) as file_error:
        invoke(0, profile, "inspect", collection=selector)
    assert file_error.value.code == expected
    for spelling in (selector, f"./{selector}", f"  {selector}  ", str(roots[1] / selector)):
        with monkeypatch.context() as guarded:
            guarded.setattr(collections, "load_manifest", lambda *a, **k: pytest.fail("file manifest read"))
            guarded.setattr(collections, "_safe_candidate_rel", lambda *a, **k: pytest.fail("file path probe"))
            with pytest.raises(OpError) as store_error:
                invoke(1, profile, "inspect", collection=spelling)
        assert store_error.value.code == expected
        if expected == "INVALID_COLLECTION_PATH":
            assert store_error.value.message == file_error.value.message
            assert "_collection.md" in file_error.value.as_public_dict()["remediation"]
            assert store_error.value.as_public_dict()["remediation"] == collections._unresolvable_reference_error(selector).details["remediation"]
    assert tuple(handle.connection.iterdump()) == before


@pytest.mark.parametrize("profile", ["records", "planning"])
def test_generic_missing_nonmanifest_selector_keeps_collection_semantics(store, profile):
    store.create_collection(manifest_path(profile), manifest_text(profile), why="create")
    before = tuple(store.connection.iterdump())
    with pytest.raises(collections.CollectionError) as caught:
        store.inspect_collection(manifest_path(profile).replace("_collection.md", "missing.md"))
    assert caught.value.code == "INVALID_COLLECTION_PATH"
    assert caught.value.details["remediation"]
    assert tuple(store.connection.iterdump()) == before


@pytest.mark.parametrize("recipe", [False, True])
@pytest.mark.parametrize("changes", [{}, {"title": "Two"}])
def test_integer_refresh_flag_uses_existing_facade_forwarding(paired, recipe, changes):
    invoke, _, handle = paired
    text = manifest_text()
    if recipe:
        text = text.removesuffix("---\n") + (
            "item_presentation:\n  version: 1\n  title: title\n  summary: [count]\n---\n"
        )
    for mode in (0, 1):
        invoke(mode, "records", "create", manifest_path=manifest_path(), manifest_text=text, why="create")
    first = [invoke(mode, "records", "append", collection=CID, item_key=KEY,
                    item={"title": "One"}, why="capture") for mode in (0, 1)]
    before = tuple(handle.connection.iterdump())
    updated = []
    for mode in (0, 1):
        args = dict(collection=CID, item_key=KEY, changes=changes, refresh_presentation=1,
                    why="correct", expected_container_hash=first[mode]["after_container_hash"],
                    expected_item_version=first[mode]["after_item_hash"])
        if changes:
            updated.append(invoke(mode, "records", "update", **args))
        else:
            with pytest.raises(OpError) as caught:
                invoke(mode, "records", "update", **args)
            assert caught.value.code == "INVALID_RECORD_CHANGES"
    if changes:
        same_wire(*updated)
        assert json.loads(handle.connection.execute("SELECT values_json FROM items").fetchone()[0])["title"] == "Two"
    else:
        assert tuple(handle.connection.iterdump()) == before


@pytest.mark.parametrize("refresh", [False, 0, "yes", [], {}])
def test_invalid_refresh_flag_keeps_facade_argument_validation(paired, refresh):
    invoke, _, handle = paired
    paired_create(paired, "records")
    first = [invoke(mode, "records", "append", collection=CID, item_key=KEY,
                    item={"title": "One"}, why="capture") for mode in (0, 1)]
    before = tuple(handle.connection.iterdump())
    for mode in (0, 1):
        with pytest.raises(TypeError if isinstance(refresh, (list, dict)) else OpError) as caught:
            invoke(mode, "records", "update", collection=CID, item_key=KEY,
                   changes={"title": "Two"}, refresh_presentation=refresh, why="correct",
                   expected_container_hash=first[mode]["after_container_hash"],
                   expected_item_version=first[mode]["after_item_hash"])
        if isinstance(caught.value, OpError):
            assert caught.value.code == "INVALID_RECORD_ARGUMENTS"
    assert tuple(handle.connection.iterdump()) == before


@pytest.mark.parametrize("scaffold", [True, False])
@pytest.mark.parametrize("identity_source", ["explicit", "derived", "derived-defaulted"])
def test_identical_planning_replay_has_exact_per_mode_outcome(paired, scaffold, identity_source):
    invoke, roots, handle = paired
    text = manifest_text("planning")
    if identity_source == "derived-defaulted":
        text = text.replace("[title]", "[title, kind]")
    for mode in (0, 1):
        invoke(mode, "planning", "create", manifest_path=manifest_path("planning"),
               manifest_text=text, scaffold=scaffold, why="create")
    if not scaffold:
        (roots[0] / manifest_path("planning")).parent.joinpath("Items").mkdir()
    args = dict(collection=CID, item={"title": "One"},
                plan_id=KEY if identity_source == "explicit" else None, why="capture")
    first = [invoke(mode, "planning", "add", **args) for mode in (0, 1)]
    same_wire(*first)
    before = tuple(handle.connection.iterdump())
    with pytest.raises(OpError) as caught:
        invoke(0, "planning", "add", **args)
    assert caught.value.code == "PLAN_ID_CONFLICT"
    replay = invoke(1, "planning", "add", **args)
    assert mutation_terminal.valid_planning_receipt(replay)
    assert mutation_terminal.valid_collection_receipt(replay)
    assert replay["outcome"] == "replayed"
    assert replay["audit_correlation"] == first[1]["audit_correlation"]
    assert replay["before_item_hash"] == replay["after_item_hash"] == first[1]["after_item_hash"]
    assert replay["before_container_hash"] == replay["after_container_hash"] == first[1]["after_container_hash"]
    assert tuple(handle.connection.iterdump()) == before
    assert handle.connection.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1
    assert handle.connection.execute("SELECT COUNT(*) FROM audit_effects").fetchone()[0] == 1
    assert handle.connection.execute("SELECT COUNT(*) FROM txns").fetchone()[0] == 2
    for overrides, expected in [
        ({"body": "Different content"}, "PLAN_ID_CONFLICT"),
        ({"plan_id": str(uuid.UUID(int=2))}, "RECORD_NATURAL_KEY_CONFLICT"),
        ({"expected_container_hash": "0" * 64}, "STALE_PLAN_CONTAINER"),
        ({"item": None}, "INVALID_PLAN_ARGUMENTS"),
    ]:
        for mode in (0, 1):
            with pytest.raises(OpError) as caught:
                invoke(mode, "planning", "add", **{**args, **overrides})
            assert caught.value.code == expected
        assert tuple(handle.connection.iterdump()) == before


@pytest.mark.parametrize("selector_kind", ["uuid", "manifest"])
@pytest.mark.parametrize("target_profile", ["planning", "records"])
def test_cross_profile_inspection_preserves_facade_capabilities(
    paired, selector_kind, target_profile
):
    invoke, _, handle = paired
    paired_create(paired, target_profile)
    selector = CID if selector_kind == "uuid" else manifest_path(target_profile)
    caller_profile = "records" if target_profile == "planning" else "planning"
    before = tuple(handle.connection.iterdump())
    try:
        if caller_profile == "records":
            for mode in (0, 1):
                inspection = invoke(mode, caller_profile, "inspect", collection=selector)
                assert inspection["kind"] == "collection"
                assert inspection["contract"]["collection_id"] == CID
                assert inspection["contract"]["semantic_profile"] == target_profile
                assert inspection["contract"]["title"] == "Work"
                assert inspection["coverage"]["committed"] == 0
        else:
            for mode in (0, 1):
                with pytest.raises(OpError) as caught:
                    invoke(mode, caller_profile, "inspect", collection=selector)
                assert caught.value.code == "PLANNING_PROFILE_REQUIRED"
    finally:
        assert tuple(handle.connection.iterdump()) == before
