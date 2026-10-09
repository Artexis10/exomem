"""File/store facade goldens with only the OpenSpec-ruled normalizations."""

import hashlib
import json
import re
from copy import deepcopy

import pytest
from test_collection_store_writer import CID, KEY, OTHER, manifest_path, manifest_text
from test_collection_store_writer import store as store

from exomem import mutation_terminal, records
from exomem import structured_collections as collections
from exomem.cli_ops import OpError
from exomem.collection_store import connection
from exomem.plan_memory import plan_memory
from exomem.record_memory import record_memory


def normalized(value):
    result = deepcopy(value)
    if not isinstance(result, dict):
        return result

    def mask(mapping, names, placeholder):
        for name in names:
            if name in mapping and mapping[name] is not None:
                mapping[name] = placeholder

    receipt = (result.get("_record_receipt") == "exomem.records-mutation"
               or result.get("_plan_receipt") == "exomem.planning-mutation")
    if receipt:
        mask(result, ("before_item_hash", "after_item_hash", "before_container_hash",
                      "after_container_hash", "before_manifest_hash", "after_manifest_hash"), "<hash>")
        mask(result, ("audit_correlation",), "<identity>")
        if result.get("receipt_version") == 2 and result.get("operation") in {"revise", "rebaseline"}:
            mask(result, ("payload_hash",), "<hash>")
    if result.get("kind") == "collection" and result.get("report_only") is True:
        mask(result, ("snapshot",), "<hash>")
        mask(result.get("lifecycle_guards", {}),
             ("expected_container_hash", "expected_manifest_hash"), "<hash>")
        for version in result.get("source_versions", []):
            mask(version, ("hash",), "<hash>")
        for view in result.get("saved_views", []):
            mask(view, ("identity",), "<identity>")
        for held in result.get("coverage", {}).get("held_refs", []):
            mask(held, ("held_at",), "<identity>")
    return result


def same_wire(left, right):
    assert json.dumps(normalized(left), sort_keys=True) == json.dumps(normalized(right), sort_keys=True)


@pytest.fixture
def paired(tmp_path, monkeypatch):
    from exomem.collection_store.preview import preview_store
    from exomem.governance.principal import library_scope

    monkeypatch.setenv("EXOMEM_COLLECTION_STORE_PREVIEW", "1")
    monkeypatch.setattr("exomem.records._capture_sweep_carrier", lambda *a, **kw: None)
    monkeypatch.setattr("exomem.records._due_state_carrier", lambda *a, **kw: None)
    roots = [tmp_path / "files", tmp_path / "store"]
    for root in roots:
        (root / "Knowledge Base").mkdir(parents=True)
        (root / "Knowledge Base/log.md").write_text("# Log\n")
    with (connection.open_writer(tmp_path / "collections.sqlite", lease_check=lambda: True) as handle,
          library_scope()):
        def invoke(mode, profile, action, **args):
            call = record_memory if profile == "records" else plan_memory
            if mode == 1:
                with preview_store(roots[mode], handle):
                    return call(roots[mode], action, **args)
            return call(roots[mode], action, **args)

        yield invoke, roots, handle


def paired_create(paired, profile, *, scaffold=True):
    invoke, roots, _ = paired
    receipts = [invoke(mode, profile, "create", manifest_path=manifest_path(profile),
                       manifest_text=manifest_text(profile), why="create", scaffold=scaffold) for mode in (0, 1)]
    same_wire(*receipts)
    if not scaffold:
        (roots[0] / manifest_path(profile)).parent.joinpath("Items").mkdir()
    return receipts


def test_dispatcher_reports_committed_collection_and_held_lifecycle(store):
    """Canonical lifecycle writes must report their durable result, not mid-flight."""
    from test_due_state_bulk_carriers import _command

    from exomem import writer_lease
    from exomem.collection_store.preview import preview_store

    def invoke(action, **kwargs):
        with preview_store(store.root, store.handle):
            result = writer_lease.invoke_command(
                _command("record_memory"), store.root, action=action,
                response_detail="full", **kwargs,
            )
        assert result["status"] == "committed"
        return result["diagnostics"]

    invoke("create", manifest_path=manifest_path(), manifest_text=manifest_text(),
           why="create", scaffold=False)
    state = store.inspect_collection(CID)
    invoke("revise", collection=CID, manifest_text=manifest_text().replace("title: Work", "title: Revised Work"),
           expected_manifest_hash=state["lifecycle_guards"]["expected_manifest_hash"],
           expected_container_hash=state["snapshot"], why="revise")
    with pytest.raises(collections.CollectionError, match="SCHEMA_FIELD_TYPE"):
        store.append_record(CID, item={"title": "Held", "count": "invalid"},
                            item_key=KEY, why="capture", hold=True)
    held = store.inspect_collection(CID)["coverage"]["held_refs"][0]["held_id"]
    invoke("discard", collection=CID, held=held, why="discard invalid candidate")
    assert store.inspect_collection(CID)["coverage"]["held"] == 0


@pytest.mark.parametrize("profile", ["records", "planning"])
def test_public_query_reads_committed_store_rows_without_rendered_files(paired, profile):
    invoke, roots, handle = paired
    paired_create(paired, profile, scaffold=False)
    action = "append" if profile == "records" else "add"
    key_arg = "item_key" if profile == "records" else "plan_id"
    for mode in (0, 1):
        invoke(mode, profile, action, collection=manifest_path(profile),
               item={"title": "One"}, **{key_arg: KEY}, why="capture")
    assert "exomem_view:" in (roots[1] / manifest_path(profile)).read_text()
    for path, in handle.connection.execute("SELECT path FROM projection_state"):
        (roots[1] / path).unlink()
    assert not (roots[1] / manifest_path(profile)).exists()
    results = [invoke(mode, profile, "query", collection=manifest_path(profile))
               for mode in (0, 1)]
    for result in results:
        assert result["total_matched"] == result["returned"] == 1
        assert result["rows"][0]["title"] == "One"
        assert result["rows"][0]["record_id" if profile == "records" else "plan_id"] == KEY


def test_bound_direct_record_query_uses_the_store_without_file_authority(paired):
    from exomem import record_governance
    from exomem.collection_store.preview import preview_store

    invoke, roots, handle = paired
    paired_create(paired, "records", scaffold=False)
    invoke(1, "records", "append", collection=CID, item={"title": "One"},
           item_key=KEY, why="capture")
    with preview_store(roots[1], handle):
        result = record_governance.query_collection(roots[1], CID)
    assert result.total_matched == 1
    assert result.rows[0]["title"] == "One"


@pytest.mark.parametrize("profile", ["records", "planning"])
def test_store_query_refuses_file_audit_history_until_canonical_reader_exists(paired, profile):
    invoke, _, _ = paired
    paired_create(paired, profile, scaffold=False)
    with pytest.raises(OpError) as caught:
        invoke(1, profile, "query", collection=CID, include_agent_history=True)
    assert caught.value.code == "COLLECTION_STORE_PREVIEW_UNSUPPORTED"


def test_direct_planning_query_refuses_file_audit_history(paired):
    from exomem import planning
    from exomem.collection_store.preview import preview_store

    invoke, roots, handle = paired
    paired_create(paired, "planning", scaffold=False)
    with preview_store(roots[1], handle):
        with pytest.raises(collections.CollectionError, match="COLLECTION_STORE_PREVIEW_UNSUPPORTED"):
            planning.query(roots[1], CID, include_agent_history=True)


def _query_parity_projection(result):
    projected = deepcopy(result)
    for key in ("snapshot", "continuation", "generated_at", "source_versions", "source_hashes"):
        projected.pop(key, None)
    for row in projected.get("rows", []):
        row.pop("item_version", None)
    if isinstance(projected.get("aggregate"), dict):
        latest = projected["aggregate"].get("row")
        if isinstance(latest, dict):
            latest.pop("item_version", None)
        profile = projected["aggregate"].get("profile")
        if isinstance(profile, dict):
            for column in profile["columns"]:
                if column["name"] == "item_version":
                    column.clear()
                    column["name"] = "item_version"
            projected["aggregate"]["dataset_card"] = re.sub(
                r"(?m)^- \*\*item_version\*\*.*\n?", "", projected["aggregate"]["dataset_card"]
            )
    if isinstance(projected.get("view"), dict):
        projected["view"].pop("identity", None)
    if isinstance(projected.get("view_provenance"), dict):
        projected["view_provenance"].pop("identity", None)
    if isinstance(projected.get("query"), dict) and isinstance(projected["query"].get("view"), dict):
        projected["query"]["view"].pop("identity", None)
    if isinstance(projected.get("rendered"), str):
        projected["rendered"] = _query_parity_projection(json.loads(projected["rendered"]))
    return projected


@pytest.mark.parametrize("profile", ["records", "planning"])
def test_file_and_store_queries_share_operator_and_aggregate_results(paired, profile):
    invoke, _, _ = paired
    paired_create(paired, profile, scaffold=False)
    action = "append" if profile == "records" else "add"
    key_arg = "item_key" if profile == "records" else "plan_id"
    for mode in (0, 1):
        for title, key in (("One", KEY), ("Two", OTHER)):
            invoke(mode, profile, action, collection=manifest_path(profile),
                   item={"title": title}, **{key_arg: key}, why="capture")
    queries = [
        {"filters": [{"column": "title", "op": op, "value": value}]}
        for op, value in (
            ("eq", "Two"), ("ne", "Two"), ("gt", "One"), ("gte", "Two"),
            ("lt", "Two"), ("lte", "Two"), ("contains", "w"),
            ("icontains", "W"), ("startswith", "T"), ("in", ["One", "Two"]),
            ("nin", ["Two"]), ("exists", None), ("missing", None),
        )
    ] + [{"aggregate": aggregate} for aggregate in (
        "count", "min:title", "max:title", "sum:title", "avg:title",
        "latest:title", "distinct:title", "group:title", "profile",
    )]
    for query in queries:
        results = [invoke(mode, profile, "query", collection=manifest_path(profile), **query)
                   for mode in (0, 1)]
        assert _query_parity_projection(results[0]) == _query_parity_projection(results[1]), query


@pytest.mark.parametrize("profile", ["records", "planning"])
def test_saved_query_view_uses_canonical_store_definition(paired, profile):
    invoke, _, _ = paired
    text = manifest_text(profile).replace(
        "lifecycle: active\n", "lifecycle: active\nviews:\n  two:\n    query:\n"
        "      filters:\n        - {column: title, op: eq, value: Two}\n",
    )
    for mode in (0, 1):
        invoke(mode, profile, "create", manifest_path=manifest_path(profile),
               manifest_text=text, why="create", scaffold=False)
        if mode == 0:
            (paired[1][0] / manifest_path(profile)).parent.joinpath("Items").mkdir()
        action = "append" if profile == "records" else "add"
        key_arg = "item_key" if profile == "records" else "plan_id"
        for title, key in (("One", KEY), ("Two", OTHER)):
            invoke(mode, profile, action, collection=CID, item={"title": title},
                   **{key_arg: key}, why="capture")
    results = [invoke(mode, profile, "query", collection=CID, view="two") for mode in (0, 1)]
    assert _query_parity_projection(results[0]) == _query_parity_projection(results[1])
    assert [row["title"] for row in results[1]["rows"]] == ["Two"]


def test_records_child_expansion_matches_file_query(paired):
    invoke, roots, _ = paired
    text = manifest_text().replace(
        "    count: {type: integer}\n", "    count: {type: integer}\n"
        "    measurements:\n      type: array\n      items: {type: object}\n",
    )
    text = text.removesuffix("---\n") + (
        "record_presentation:\n  version: 1\n  summary: [title]\n"
        "  tables:\n    - field: measurements\n      label: Measurements\n"
        "      columns:\n        - {field: name, type: string}\n---\n"
    )
    for mode in (0, 1):
        invoke(mode, "records", "create", manifest_path=manifest_path(),
               manifest_text=text, why="create", scaffold=False)
        if mode == 0:
            (roots[0] / manifest_path()).parent.joinpath("Items").mkdir()
        invoke(mode, "records", "append", collection=CID,
               item={"title": "One", "measurements": [{"name": "First"}, {"name": "Second"}]},
               item_key=KEY, why="capture")
    results = [invoke(mode, "records", "query", collection=CID, expand_children=True)
               for mode in (0, 1)]
    assert _query_parity_projection(results[0]) == _query_parity_projection(results[1])
    assert [row["name"] for row in results[1]["rows"]] == ["First", "Second"]


@pytest.mark.parametrize("insertion", ["newest-first", "oldest-first"])
def test_markdown_log_query_preserves_declared_order_and_notes(paired, insertion):
    """Bounded pages preserve log order and notes declared outside item_schema."""
    from exomem import record_governance
    from exomem.collection_store.preview import preview_store

    invoke, roots, handle = paired
    text = (manifest_text()
            .replace("strategy: markdown-items\n  source: Items", "strategy: markdown-log\n  source: Log.md")
            .replace("  format_version: 1\n", "  format_version: 1\n"
                     "  section: {level: 2, title: Entries}\n"
                     "  item_heading:\n    level: 3\n"
                     "    fields: [{name: title, type: string}]\n"
                     '    separator: " · "\n'
                     '    note: {field: note, open: " (", close: ")"}\n'
                     "  child_rows:\n    prefix: \"- \"\n    delimiter: \"|\"\n"
                     "    fields: [field, value]\n    container_field: details\n"
                     f"  insertion: {insertion}\n")
            .replace("    count: {type: integer}",
                     "    details: {type: array, items: {type: object}}"))
    for mode in (0, 1):
        invoke(mode, "records", "create", manifest_path=manifest_path(),
               manifest_text=text, why="create")
        for title, key in (("One", KEY), ("Two", OTHER)):
            invoke(mode, "records", "append", collection=CID,
                   item={"title": title, "details": [], "note": "Authored note"},
                   item_key=key, why="capture")
    with handle.transaction() as conn:
        conn.execute("UPDATE items SET values_json=json_set(values_json, '$.undeclared', 'Private metadata')")
    pages = []
    for mode in (0, 1):
        first = invoke(mode, "records", "query", collection=CID, limit=1)
        second = invoke(mode, "records", "query", collection=CID, limit=1,
                        continuation=first["continuation"])
        pages.append([first, second])
    expected = ["Two", "One"] if insertion == "newest-first" else ["One", "Two"]
    for mode_pages in pages:
        assert [page["rows"][0]["title"] for page in mode_pages] == expected
        assert [page["rows"][0].get("note") for page in mode_pages] == ["Authored note"] * 2
        assert all(page["total_matched"] == 2 and page["returned"] == 1 for page in mode_pages)
        assert mode_pages[1]["continuation"] is None
        assert "Private metadata" not in str(mode_pages)
    for file_page, store_page in zip(*pages, strict=True):
        assert _query_parity_projection(file_page) == _query_parity_projection(store_page)
    with preview_store(roots[1], handle):
        direct = record_governance.query_collection(roots[1], CID)
    assert [row["title"] for row in direct.rows] == expected
    assert [row.get("note") for row in direct.rows] == ["Authored note"] * 2
    assert "Private metadata" not in str(direct.rows)


def test_numeric_date_and_list_queries_match_file_mode(paired):
    invoke, roots, _ = paired
    text = manifest_text().replace(
        "    count: {type: integer}\n", "    count: {type: integer}\n"
        "    occurred_on: {type: date}\n"
        "    tags: {type: array, items: {type: string}}\n"
        "    flag: {type: boolean}\n"
        "    related: {type: link}\n",
    )
    for mode in (0, 1):
        target = roots[mode] / "Knowledge Base/Notes/Target.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# Target\n")
        invoke(mode, "records", "create", manifest_path=manifest_path(),
               manifest_text=text, why="create", scaffold=False)
        if mode == 0:
            (roots[0] / manifest_path()).parent.joinpath("Items").mkdir()
        for title, key, count, date, tags, flag in (
            ("One", KEY, 2, "2026-01-01", ["first"], False),
            ("Two", OTHER, 4, "2026-02-01", ["second"], True),
        ):
            invoke(mode, "records", "append", collection=CID,
                   item={"title": title, "count": count, "occurred_on": date,
                         "tags": tags, "flag": flag, "related": "[[Notes/Target]]"},
                   item_key=key, why="capture")
    for query in (
        {"filters": [{"column": "count", "op": "gte", "value": 3}]},
        {"date_from": "2026-02-01", "date_column": "occurred_on"},
        {"filters": [{"column": "tags", "op": "contains", "value": "second"}]},
        {"filters": [{"column": "flag", "op": "eq", "value": True}]},
        {"filters": [{"column": "related", "op": "eq", "value": "[[Notes/Target]]"}]},
        {"aggregate": "sum:count"},
        {"aggregate": "latest:occurred_on"},
    ):
        results = [invoke(mode, "records", "query", collection=CID, **query)
                   for mode in (0, 1)]
        assert _query_parity_projection(results[0]) == _query_parity_projection(results[1])
        if query.get("filters", [{}])[0].get("column") == "related":
            assert results[1]["total_matched"] == 2
        if query.get("filters", [{}])[0].get("column") == "flag":
            assert [row["title"] for row in results[1]["rows"]] == ["Two"]


@pytest.mark.parametrize("output_format", ["markdown", "csv"])
def test_rendered_query_formats_match_file_mode(paired, output_format):
    invoke, _, _ = paired
    paired_create(paired, "records", scaffold=False)
    for mode in (0, 1):
        invoke(mode, "records", "append", collection=CID, item={"title": "One"},
               item_key=KEY, why="capture")
    rendered = [invoke(mode, "records", "query", collection=CID,
                       output_format=output_format)["rendered"] for mode in (0, 1)]
    normalized = [re.sub(r"[0-9a-f]{64}|(?<=generated_at: )[^\n]+", "<mode-local>",
                         value) for value in rendered]
    assert normalized[0] == normalized[1]


@pytest.mark.parametrize("hierarchy_mode", ["ancestors", "descendants"])
def test_planning_hierarchy_matches_file_query(paired, hierarchy_mode):
    invoke, _, _ = paired
    paired_create(paired, "planning", scaffold=False)
    for mode in (0, 1):
        invoke(mode, "planning", "add", collection=CID,
               item={"title": "Parent", "kind": "outcome"}, plan_id=KEY, why="capture")
        invoke(mode, "planning", "add", collection=CID,
               item={"title": "Child", "kind": "initiative", "parent": f"exomem://plan/{CID}/{KEY}"},
               plan_id=OTHER, why="capture")
    root = OTHER if hierarchy_mode == "ancestors" else KEY
    results = [invoke(mode, "planning", "query", collection=CID,
                      filters=[{"column": "plan_id", "op": "eq", "value": root}],
                      hierarchy_mode=hierarchy_mode) for mode in (0, 1)]
    assert _query_parity_projection(results[0]) == _query_parity_projection(results[1])
    assert results[1]["hierarchy"]["edges"] == [{"parent": KEY, "child": OTHER}]


@pytest.mark.parametrize("profile", ["records", "planning"])
def test_public_facade_create_append_replay_update_and_refusals(paired, profile):
    invoke, _, _ = paired
    paired_create(paired, profile)
    key_arg = "item_key" if profile == "records" else "plan_id"
    valid = mutation_terminal.valid_record_receipt if profile == "records" else mutation_terminal.valid_planning_receipt
    action = "append" if profile == "records" else "add"
    common = dict(collection=manifest_path(profile), why="capture")
    first = [invoke(mode, profile, action, item={"title": "One"}, **{key_arg: KEY}, **common) for mode in (0, 1)]
    same_wire(*first)
    replay = []
    if profile == "records":
        replay = [invoke(mode, profile, action, item={"title": "One"}, **{key_arg: KEY}, **common) for mode in (0, 1)]
        same_wire(*replay)
    for candidate, key in [({"title": "Changed"}, KEY), ({"title": "One"}, OTHER)]:
        codes = []
        for mode in (0, 1):
            with pytest.raises(OpError) as caught:
                invoke(mode, profile, action, item=candidate, **{key_arg: key}, **common)
            codes.append(caught.value.code)
        assert codes[0] == codes[1]
    for guard in ("expected_container_hash", "expected_item_version"):
        codes = []
        for mode in (0, 1):
            args = dict(expected_container_hash=first[mode]["after_container_hash"],
                        expected_item_version=first[mode]["after_item_hash"], **{key_arg: KEY}, changes={"title": "Two"})
            args[guard] = "0" * 64
            with pytest.raises(OpError) as caught:
                invoke(mode, profile, "update", **args, **common)
            codes.append(caught.value.code)
        assert codes[0] == codes[1]
    updated = [invoke(mode, profile, "update", **{key_arg: KEY}, changes={"title": "Two"},
                      expected_container_hash=first[mode]["after_container_hash"],
                      expected_item_version=first[mode]["after_item_hash"], **common) for mode in (0, 1)]
    same_wire(*updated)
    for receipt in first + replay + updated:
        assert valid(receipt) and mutation_terminal.valid_collection_receipt(receipt)
    errors = []
    for mode in (0, 1):
        with pytest.raises(OpError) as caught:
            invoke(mode, profile, "inspect", item={"title": "Wrong action"}, **common)
        errors.append(caught.value)
    assert errors[0].code == errors[1].code
    assert str(errors[0]) == str(errors[1])


@pytest.mark.parametrize("profile", ["records", "planning"])
def test_revision_hash_parity_obeys_the_ruled_normalization(paired, profile):
    from exomem import record_governance
    from exomem.collection_store.preview import preview_store

    invoke, roots, handle = paired
    made = paired_create(paired, profile)
    path, text = manifest_path(profile), manifest_text(profile)
    proposed = text.replace("title: Work", "title: Revised")
    before = [record_governance.inspect_collection(roots[0], path)["lifecycle_guards"], None]
    with preview_store(roots[1], handle) as writer:
        before[1] = {"expected_manifest_hash": writer._collection(CID)[1].manifest_version.hash,
                     "expected_container_hash": made[1]["after_container_hash"]}
    receipts = [invoke(mode, profile, "revise", collection=path, manifest_text=proposed,
                       why="revise", **before[mode]) for mode in (0, 1)]
    same_wire(*receipts)
    proposed_hash = hashlib.sha256(proposed.encode()).hexdigest()
    for mode, receipt in enumerate(receipts):
        assert mutation_terminal.valid_collection_receipt(receipt)
        assert receipt["before_manifest_hash"] == before[mode]["expected_manifest_hash"]
        assert receipt["before_container_hash"] == before[mode]["expected_container_hash"]
        assert receipt["payload_hash"] == records.lifecycle_request_hash(
            action="revise", collection_id=CID, before_manifest_hash=receipt["before_manifest_hash"],
            before_container_hash=receipt["before_container_hash"], proposed_manifest_hash=proposed_hash,
            acknowledged_gap_codes=(), rationale="revise")
    assert receipts[0]["after_manifest_hash"] == collections.load_manifest(roots[0], path).manifest_version.hash
    assert receipts[1]["after_manifest_hash"] == handle.connection.execute(
        "SELECT manifest_hash FROM collection_manifests ORDER BY manifest_version DESC LIMIT 1").fetchone()[0]


@pytest.mark.parametrize("profile", ["records", "planning"])
def test_inspect_shape_guards_and_store_only_projection(paired, profile):
    invoke, roots, handle = paired
    paired_create(paired, profile)
    key_arg = "item_key" if profile == "records" else "plan_id"
    for mode in (0, 1):
        invoke(mode, profile, "append" if profile == "records" else "add", collection=manifest_path(profile),
               item={"title": "One"}, **{key_arg: KEY}, why="capture")
    before = handle.connection.total_changes
    inspections = [invoke(mode, profile, "inspect", collection=manifest_path(profile)) for mode in (0, 1)]
    assert handle.connection.total_changes == before
    stored = inspections[1]
    # Field-release discovery is store-specific; compare the shared inspection below.
    basis = stored.pop("field_release_basis")
    assert basis["version"] == 1
    assert basis["store_id"] == handle.connection.execute(
        "SELECT value FROM store_meta WHERE key='store_id'").fetchone()[0]
    assert basis["collection_id"] == CID
    assert basis["path"] == basis["ref"] == manifest_path(profile)
    assert basis["fields"] == list(collections.load_manifest(roots[0], manifest_path(profile)).schema.fields)
    assert len(basis["classification_basis"]) == 64
    assert stored["lifecycle_guards"]["expected_manifest_hash"] == handle.connection.execute(
        "SELECT manifest_hash FROM collection_manifests ORDER BY manifest_version DESC LIMIT 1").fetchone()[0]
    assert stored["lifecycle_guards"]["expected_container_hash"] == stored["snapshot"]
    assert all(set(entry) == {"path", "hash"} for entry in stored["source_versions"])
    assert {d["code"] for d in stored["diagnostics"]} == {"PROJECTION_PENDING"}
    if profile == "records":
        assert stored.pop("projection") == {"pending_views": 2, "held_view_corrections": 0}
    else:
        assert set(stored) == set(inspections[0])
    stored["diagnostics"] = []
    text, stored_hash = handle.connection.execute(
        "SELECT manifest_text, manifest_hash FROM collection_manifests ORDER BY manifest_version DESC LIMIT 1"
    ).fetchone()
    manifests = [
        collections.load_manifest(roots[0], manifest_path(profile)),
        collections.parse_manifest_bytes(roots[1], manifest_path(profile), text.encode()),
    ]
    assert manifests[1].manifest_version.hash == stored_hash
    for manifest, inspection in zip(manifests, inspections, strict=True):
        if profile == "planning":
            assert inspection["saved_views"], "Planning declares its default saved views"
        for view in inspection["saved_views"]:
            assert view["identity"] == collections.resolve_saved_view(manifest, view["name"]).identity
    same_wire(*inspections)
    proposed = manifest_text(profile).replace("title: Work", "title: Revised")
    for mode, inspection in enumerate(inspections):
        receipt = invoke(mode, profile, "revise", collection=manifest_path(profile),
                         manifest_text=proposed, why="revise", **inspection["lifecycle_guards"])
        assert receipt["before_manifest_hash"] == inspection["lifecycle_guards"]["expected_manifest_hash"]


def test_records_held_rehold_resume_update_and_discard_wire(paired):
    invoke, _, handle = paired
    made = paired_create(paired, "records")
    common = dict(collection=manifest_path(), why="capture")
    held_details = []
    for mode in (0, 1):
        with pytest.raises(OpError) as caught:
            invoke(mode, "records", "append", item={"title": "One", "count": "bad"}, item_key=KEY, **common)
        held_details.append(caught.value.details)
    same_wire(*held_details)
    refs = [d["held"]["held_id"] for d in held_details]
    for mode in (0, 1):
        with pytest.raises(OpError) as caught:
            invoke(mode, "records", "append", held=refs[mode], item={"count": "still bad"}, **common)
        assert caught.value.details["held"]["held_id"] == refs[mode]
    assert handle.connection.execute("SELECT COUNT(*) FROM txns").fetchone()[0] == 1
    resumed = [invoke(mode, "records", "append", held=refs[mode], item={"count": 1},
                      expected_container_hash=made[mode]["after_container_hash"], **common) for mode in (0, 1)]
    same_wire(*resumed)
    keys = [receipt["item_key"] for receipt in resumed]
    refs = []
    for mode in (0, 1):
        with pytest.raises(OpError) as caught:
            invoke(mode, "records", "update", item_key=keys[mode], changes={"count": "bad"},
                   expected_container_hash=resumed[mode]["after_container_hash"],
                   expected_item_version=resumed[mode]["after_item_hash"], **common)
        refs.append(caught.value.details["held"]["held_id"])
    updated = [invoke(mode, "records", "update", item_key=keys[mode], held=refs[mode], changes={"count": 2},
                      expected_container_hash=resumed[mode]["after_container_hash"],
                      expected_item_version=resumed[mode]["after_item_hash"], **common) for mode in (0, 1)]
    same_wire(*updated)
    refs = []
    for mode in (0, 1):
        with pytest.raises(OpError) as caught:
            invoke(mode, "records", "append", item={"title": "Two", "count": "bad"}, **common)
        refs.append(caught.value.details["held"]["held_id"])
    discarded = [invoke(mode, "records", "discard", held=refs[mode], **common) for mode in (0, 1)]
    same_wire(*discarded)
    for receipt in resumed + updated + discarded:
        assert mutation_terminal.valid_record_receipt(receipt) and mutation_terminal.valid_collection_receipt(receipt)
    assert handle.connection.execute("SELECT COUNT(*) FROM held_candidates").fetchone()[0] == 0


def test_planning_triage_and_hierarchy_refusal_wire(paired):
    invoke, _, handle = paired
    paired_create(paired, "planning")
    common = dict(collection=manifest_path("planning"), why="capture", plan_id=KEY)
    first = [invoke(mode, "planning", "add", item={"title": "One"}, **common) for mode in (0, 1)]
    triaged = [invoke(mode, "planning", "triage", transition={"horizon": "week", "status": "planned", "commitment": "considering"},
                      expected_container_hash=first[mode]["after_container_hash"],
                      expected_item_version=first[mode]["after_item_hash"], **common) for mode in (0, 1)]
    same_wire(*triaged)
    for receipt in triaged:
        assert mutation_terminal.valid_planning_receipt(receipt) and mutation_terminal.valid_collection_receipt(receipt)
    errors = []
    for mode in (0, 1):
        with pytest.raises(OpError) as caught:
            invoke(mode, "planning", "add", collection=manifest_path("planning"), why="capture",
                   plan_id=OTHER, item={"title": "Child", "kind": "work-item", "parent": KEY})
        errors.append(caught.value)
    assert errors[0].code == errors[1].code == "INVALID_PLAN"
    assert handle.connection.execute("SELECT COUNT(*) FROM txns").fetchone()[0] == 3


@pytest.mark.parametrize("field,value", [
    ("snapshot", "A"), ("identity", "A"), ("payload_hash", "A"),
    ("nested", {"path": "authored", "hash": "A"}),
    ("nested", {"name": "authored", "definition": {}, "identity": "A"}),
    ("nested", {"operation": "revise", "payload_hash": "A"}),
])
def test_normalizer_preserves_authored_observed_values(paired, field, value):
    invoke, _, _ = paired
    field_type = "object" if isinstance(value, dict) else "string"
    text = manifest_text().replace("    count: {type: integer}", f"    {field}: {{type: {field_type}}}")
    invoke(1, "records", "create", manifest_path=manifest_path(), manifest_text=text, why="create")
    invoke(1, "records", "append", collection=CID, item_key=KEY,
           item={"title": "One", field: value}, why="capture")
    inspection = invoke(1, "records", "inspect", collection=CID)
    changed = deepcopy(inspection)
    changed["observed_values"][field] = {"values": [{"value": "B", "count": 1}]}
    assert normalized(inspection) != normalized(changed)
    assert normalized(inspection)["observed_values"] == inspection["observed_values"]


@pytest.mark.parametrize("authored", [
    {"snapshot": "A"}, {"identity": "A"}, {"payload_hash": "A"},
    {"path": "authored", "hash": "A"},
    {"name": "authored", "definition": {}, "identity": "A"},
    {"operation": "revise", "payload_hash": "A"},
])
def test_normalizer_preserves_saved_view_definitions_and_nested_lookalikes(authored):
    payload = {"kind": "collection", "report_only": True, "snapshot": "mode-local",
               "saved_views": [{"name": "one", "definition": authored, "identity": "mode-local"}],
               "observed_values": {"nested": authored}, "other": authored}
    result = normalized(payload)
    assert result["saved_views"][0]["definition"] == authored
    assert result["observed_values"]["nested"] == authored
    assert result["other"] == authored


def test_normalizer_requires_receipt_kind_for_lifecycle_payload_hash():
    authored = {"operation": "revise", "payload_hash": "authored"}
    assert normalized(authored) == authored


def test_records_nonempty_saved_views_match_definitions_and_mode_local_identity(paired):
    invoke, roots, handle = paired
    text = manifest_text().removesuffix("---\n") + (
        "views:\n  one:\n    query:\n      filters: [{column: title, op: eq, value: One}]\n---\n"
    )
    for mode in (0, 1):
        invoke(mode, "records", "create", manifest_path=manifest_path(), manifest_text=text, why="create")
    inspections = [invoke(mode, "records", "inspect", collection=CID) for mode in (0, 1)]
    stored_text = handle.connection.execute("SELECT manifest_text FROM collection_manifests").fetchone()[0]
    manifests = [collections.load_manifest(roots[0], manifest_path()),
                 collections.parse_manifest_bytes(roots[1], manifest_path(), stored_text.encode())]
    for manifest, inspection in zip(manifests, inspections, strict=True):
        assert len(inspection["saved_views"]) == 1
        view = inspection["saved_views"][0]
        assert set(view) == {"name", "definition", "identity"}
        assert view["name"] == "one"
        assert view["identity"] == collections.resolve_saved_view(manifest, "one").identity
    same_wire(inspections[0]["saved_views"][0]["definition"], inspections[1]["saved_views"][0]["definition"])
    # Owner field-release discovery is store-specific; saved-view content remains identical.
    assert inspections[1].pop("field_release_basis")["collection_id"] == CID
    for inspection in inspections:
        inspection.pop("projection", None)
        inspection["diagnostics"] = []
    same_wire(*inspections)
