"""S1 field release keeps row-authorized metrics independent of protected source values."""

from dataclasses import replace

import pytest
import yaml
from conftest import initialize_vault_state_offline
from test_collection_store_summary import summary_text
from test_collection_store_writer import CID, KEY, OTHER, manifest_path, manifest_text
from test_collection_store_writer import store as store

from exomem import commands, vault
from exomem.collection_store.preview import preview_store
from exomem.collection_store.reader import StoreAdapter
from exomem.governance.principal import (
    RequestPrincipal,
    owner_principal,
    request_scope,
    resolve_hosted_principal,
)
from exomem.query_engine import route, runtime
from exomem.writer_lease import invoke_command

OWNER = owner_principal(surface="mcp")
GUEST = RequestPrincipal("principal:" + "ab" * 32, surface="mcp", issuer_family="mcp-oauth:fixture")
GRANT = "01ARZ3NDEKTSV4RRFFQ69G5FB1"


@pytest.fixture(autouse=True)
def owner():
    with request_scope(OWNER):
        yield


def create(store):
    front, body, _ = vault.parse_frontmatter(summary_text(manifest_text()), strict=True)
    front["item_schema"]["fields"].update({
        "count": {"type": "integer", "sortable": True},
        "place": {"type": "object", "classification": "location", "properties": {"label": {"type": "string"}}},
        "label": {"type": "string", "depends_on": ["place"]},
        "context": {"type": "object"},
        "mixed": {"type": "object", "properties": {
            "metric": {"type": "integer"}, "private": {"type": "string", "classification": "location"}}},
    })
    text = "---\n" + yaml.safe_dump(front, sort_keys=False) + "---\n" + body
    store.create_collection(manifest_path(), text, why="declare reviewed fields", scaffold=False)
    receipts = []
    for key, count in ((KEY, 1), (OTHER, 2)):
        receipts.append(store.append_record(CID, item={"title": key, "count": count,
            "place": {"label": "invented-place", "future": "private"}, "label": "invented-place",
            "context": {"late": "private"}, "mixed": {"metric": count, "private": "private", "late": "private"}},
            item_key=key, why="record invented values"))
    return receipts


def query(store, raw, who=GUEST):
    with request_scope(who):
        return route.run(store, CID, {"version": 1, **raw}, facade_profile="records")


def tool(store, name, *, who=OWNER, **arguments):
    command = next(command for command in commands.PRODUCT_COMMANDS if command.name == name)
    with request_scope(who), preview_store(store.root, store.handle):
        return invoke_command(command, store.root, **arguments)


def govern(store, **arguments):
    command = next(command for command in commands.PRODUCT_COMMANDS if command.name == "govern_memory")
    with request_scope(OWNER), preview_store(store.root, store.handle):
        result = invoke_command(command, store.root, **arguments)
        assert result["ok"], result
        return result["diagnostics"]


def release_document(basis, who=GUEST):
    return yaml.safe_dump({"governance_version": 1, "id": GRANT, "kind": "collection-fields",
        "path": basis["path"], "ref": basis["ref"], "content_hash": basis["classification_basis"],
        "to_audience": who.audience_id, "released_at": "2026-10-07T00:00:00Z", "why": "Review invented location",
        "field_release": {**{key: basis[key] for key in ("version", "store_id", "collection_id", "classification_basis")},
            "paths": [{"path": "place", "subtree": True}, {"path": "label", "subtree": False}],
            "surface": who.surface, "issuer_family": who.issuer_family, "purpose": None, "release_version": 1}})


def test_recipient_queries_metrics_and_nested_projection_before_hidden_value_decoding(store, monkeypatch):
    """A default or explicit read that hydrates location or an undeclared residual before masking it."""
    create(store)
    from exomem.collection_store import typed_storage
    original = typed_storage.decode_value

    def decode(tag, value, key):
        assert not (isinstance(value, str) and "private" in value), "withheld value reached the decoder"
        return original(tag, value, key)

    monkeypatch.setattr(typed_storage, "decode_value", decode)
    result = query(store, {"order_by": [{"field": "count"}]})
    assert [row["count"] for row in result["rows"]] == [1, 2]
    assert [row["mixed"] for row in result["rows"]] == [{"metric": 1}, {"metric": 2}]
    assert all(not {"place", "label", "context"} & row.keys() for row in result["rows"])
    assert query(store, {"aggregates": {"n": {"op": "count"}, "total": {"op": "sum", "field": "count"}}})["groups"] == [{"n": 2, "total": 3}]
    for field in ("place", "label", "context", "absent"):
        with pytest.raises(route.Refusal) as error:
            query(store, {"select": [field]})
        assert error.value.code == "QUERY_FIELD_UNAVAILABLE"


@pytest.mark.parametrize("raw", [
    {"select": ["FIELD"]}, {"where": {"field": "FIELD", "op": "exists"}},
    {"order_by": [{"field": "FIELD"}]}, {"group_by": [{"field": "FIELD"}], "aggregates": {"n": {"op": "count"}}},
    {"aggregates": {"n": {"op": "count", "field": "FIELD"}}},
    {"aggregates": {"n": {"op": "count"}}, "having": {"field": "FIELD", "op": "eq", "value": 1}},
])
def test_hidden_and_absent_operator_requests_have_the_same_refusal(store, raw):
    """An operator or discovery diagnostic that distinguishes a protected field from an absent field."""
    create(store)
    import json
    errors = []
    for name in ("label", "absent"):
        with pytest.raises(route.Refusal) as error:
            query(store, json.loads(json.dumps(raw).replace("FIELD", name)))
        errors.append(route.details(error.value))
    assert errors[0] == errors[1] and errors[0]["code"] == "QUERY_FIELD_UNAVAILABLE"


def test_location_edits_and_removal_preserve_metrics_cursor_legacy_snapshot_and_inspection(store):
    """Private-only edits that stale a metric continuation or leak through legacy provenance or inspection."""
    receipts = create(store)
    raw = {"select": ["count", "mixed"], "order_by": [{"field": "count"}], "page": {"limit": 1}}
    first = query(store, raw)
    with request_scope(GUEST), preview_store(store.root, store.handle):
        before = store.inspect_collection(CID)
        with store.read_collection(CID) as manifest:
            old = StoreAdapter(store, manifest, None).read()
    changed = store.update_record(CID, item_key=OTHER, changes={"place": {"label": "elsewhere"}, "label": None,
             "mixed": {"metric": 2, "private": "corrected", "late": "changed"}},
        why="correct private values", expected_item_version=receipts[-1]["after_item_hash"],
        expected_container_hash=receipts[-1]["after_container_hash"])
    assert changed["outcome"] == "committed"
    second = query(store, {**raw, "page": {"limit": 1, "after": first["next_cursor"]}})
    assert second["rows"] == [{"count": 2, "mixed": {"metric": 2}}]
    with request_scope(GUEST), preview_store(store.root, store.handle):
        assert store.inspect_collection(CID) == before
        with store.read_collection(CID) as manifest:
            current = StoreAdapter(store, manifest, None).read()
        assert current.snapshot == old.snapshot and current.source_versions == old.source_versions
        assert all("place" not in record.values and "label" not in record.values for record in current.records)


def test_public_owner_release_and_revoke_leave_metric_continuation_valid(store):
    """A field grant that bypasses owner authority, depends on view bytes, releases RAW, or stales unrelated metrics."""
    initialize_vault_state_offline(store.root, source="field release fixture")
    create(store)
    raw = {"select": ["count"], "order_by": [{"field": "count"}], "page": {"limit": 1}}
    first = query(store, raw)
    basis = tool(store, "record_memory", action="inspect", collection=CID)["field_release_basis"]
    proposal = govern(store, operation="propose", intent="Release reviewed invented location",
                   documents={"grants/collection-location.yaml": release_document(basis)},
                   selector_paths=[basis["path"]], target_ceiling=6, duration="standing")
    assert "proposal_id" in proposal, proposal
    govern(store, operation="commit", proposal_id=proposal["proposal_id"])
    assert query(store, {"select": ["place", "label"]})["rows"][0]["place"]["label"] == "invented-place"
    assert query(store, {**raw, "page": {"limit": 1, "after": first["next_cursor"]}})["rows"] == [{"count": 2}]
    location_query = {"select": ["label"], "order_by": [{"field": "count"}], "page": {"limit": 1}}
    location = query(store, location_query)
    govern(store, operation="revoke", scope="standing", grant_id=GRANT)
    with pytest.raises(route.Refusal, match="QUERY_FIELD_UNAVAILABLE"):
        query(store, {**location_query, "page": {"limit": 1, "after": location["next_cursor"]}})
    with pytest.raises(route.Refusal, match="QUERY_FIELD_UNAVAILABLE"):
        query(store, {"select": ["place"]})
    assert query(store, {**raw, "page": {"limit": 1, "after": first["next_cursor"]}})["rows"] == [{"count": 2}]


def test_full_fields_require_verified_owner_not_hosted_raw_exemption(store):
    """A hosted tenant or owner label that receives location via RAW's separate admission exemption."""
    create(store)
    for who in (OWNER, owner_principal(surface="rest")):
        assert query(store, {"select": ["place"]}, who)["rows"][0]["place"]["future"] == "private"
    for who in (resolve_hosted_principal("tenant:unrelated"), replace(OWNER, issuer_family=None)):
        with pytest.raises(runtime.QueryError, match="QUERY_FIELD_UNAVAILABLE"):
            query(store, {"select": ["place"]}, who)


def test_mapping_coverage_keeps_late_source_subtrees_and_extracted_aliases_private(store):
    """A mapping reviewed on the preview sample that releases a later subtree or a location alias."""
    from copy import deepcopy

    from test_collection_store_importer import CID as IMPORT_CID
    from test_collection_store_importer import (
        MAPPING,
        SOURCE,
        WORKOUT_FIELDS,
        call,
        ndjson,
        run,
        setup,
        start,
        status,
    )

    fields = WORKOUT_FIELDS + (
        "    place: {type: object, classification: location, properties: {label: {type: string}}}\n"
        "    alias: {type: string}\n    context: {type: object}\n")
    mapping = deepcopy(MAPPING)
    mapping["fields"].update(place="place", alias="place.label", context="context")
    mapping["coverage"]["place"] = {"classification": "location", "subtree": True}
    rows = [{"id": str(n), "kind": "invented", "duration_s": 10, "metrics": {"calories": n, "distance_m": 1},
             "start": "2026-03-01T00:00:00+00:00", "place": {"label": "private"}} for n in range(101)]
    rows[-1]["context"] = {"surprise": {"coordinates": [12, 34]}}
    setup(store, ndjson(rows), fields=fields)
    preview = call(store, OWNER, mode="preview", source_ref=SOURCE, format="ndjson", mapping=mapping)
    assert preview["rows"]["sampled"] == 100 and "context" not in preview["fields"]
    job = start(store, OWNER, mapping=mapping)
    run(store)
    assert status(store, job, OWNER)["rows"]["imported"] == 101
    with request_scope(GUEST):
        result = route.run(store, IMPORT_CID, {"version": 1, "select": ["calories"], "page": {"limit": 200}}, facade_profile="records")
        assert sorted(row["calories"] for row in result["rows"]) == list(range(101))
        for hidden in ("place", "alias", "context"):
            with pytest.raises(route.Refusal, match="QUERY_FIELD_UNAVAILABLE"):
                route.run(store, IMPORT_CID, {"version": 1, "select": [hidden]}, facade_profile="records")
    with store.read_collection(IMPORT_CID) as manifest:
        assert manifest.schema.fields["place"].classification == "location"
        assert manifest.schema.fields["place"].properties["label"].type == "string"


def test_summary_read_uses_admitted_count_while_file_stamps_stay_private(store):
    """A held or republished location-only correction that changes a recipient summary's output or availability."""
    from exomem import recall_policy
    from exomem.governance import egress

    receipts = create(store)
    store.reconcile_views()
    path = "Knowledge Base/Records/Work/Items/_summary.md"
    assert not recall_policy.is_recall_candidate(store.root, path)
    described = tool(store, "record_memory", who=GUEST, action="query", collection=CID,
                     query={"version": 1, "mode": "preview", "page": {"limit": 1}})
    assert set(described["plan"]["fields"]) == {"title", "count", "mixed"}
    with pytest.raises(ValueError, match="NOT_FOUND"):
        tool(store, "read_memory", who=GUEST, path=manifest_path())
    before = tool(store, "read_memory", who=GUEST, path=path)
    assert before["frontmatter"]["metric"] == "released rows" and before["frontmatter"]["value"] == 2
    owner = tool(store, "read_memory", path=path)
    assert "exomem_view" in owner["frontmatter"]
    with request_scope(GUEST), preview_store(store.root, store.handle):
        assert not egress.release_allows_download(store.root, path)
        with store.read_snapshot():
            assert not store._operation.allows_file(manifest_path())
    store.update_record(CID, item_key=OTHER, changes={"place": None}, why="remove private location",
        expected_item_version=receipts[-1]["after_item_hash"], expected_container_hash=receipts[-1]["after_container_hash"])
    assert tool(store, "read_memory", who=GUEST, path=path) == before
    store.reconcile_views()
    assert tool(store, "read_memory", who=GUEST, path=path) == before


def test_live_session_expiry_and_classification_widening_remove_field_release(store, monkeypatch):
    """A durable field grant that survives its recipient session or a changed canonical classification."""
    from test_collection_store_governance import session

    initialize_vault_state_offline(store.root, source="field authority fixture")
    create(store)
    recipient = RequestPrincipal("principal:person-1", surface="mcp", issuer_family="mcp-oauth")
    basis = tool(store, "record_memory", action="inspect", collection=CID)["field_release_basis"]
    proposal = govern(store, operation="propose", intent="Release classified fields to the active session",
        documents={"grants/session-fields.yaml": release_document(basis, recipient)},
        selector_paths=[basis["path"]], target_ceiling=6, duration="standing")
    govern(store, operation="commit", proposal_id=proposal["proposal_id"])
    who = session(store, monkeypatch, paths="Unrelated/**")
    assert query(store, {"select": ["place"]}, who)["rows"][0]["place"]["label"] == "invented-place"
    now = __import__("time").time()
    monkeypatch.setattr("time.time", lambda: who.verified_authorization_session.expires_at + 1)
    from exomem.structured_collections import CollectionError

    with pytest.raises(CollectionError, match="COLLECTION_NOT_FOUND"):
        query(store, {"select": ["place"]}, who)
    monkeypatch.setattr("time.time", lambda: now)
    inspected = tool(store, "record_memory", action="inspect", collection=CID)
    text = store.connection.execute("SELECT manifest_text FROM collection_manifests ORDER BY manifest_version DESC LIMIT 1").fetchone()[0]
    front, body, _ = vault.parse_frontmatter(text, strict=True)
    front["item_schema"]["fields"]["count"]["classification"] = "location"
    store.revise_collection(CID, manifest_text="---\n" + yaml.safe_dump(front) + "---\n" + body,
        **inspected["lifecycle_guards"], why="widen the reviewed classification")
    for field in ("place", "count"):
        with pytest.raises(runtime.QueryError, match="QUERY_FIELD_UNAVAILABLE"):
            query(store, {"select": [field]}, who)
    assert query(store, {"select": ["count"]}, OWNER)["returned"] == 2


def test_recipient_summary_counts_only_rows_released_by_ordinary_policy(store):
    """A summary that counts withheld rows, or exposes their versions through its canonical membership basis."""
    from test_governance_egress import write_rule, write_scope

    receipts = create(store)
    store.reconcile_views()
    write_scope(store.root, paths="Unrelated/**")
    scope = store.root / "Knowledge Base/_Governance/scopes/patterns.yaml"
    declaration = scope.read_text()
    scope.write_text(declaration + f"refs: [exomem://record/{CID}/{KEY}]\n")
    write_rule(store.root, ceiling=0, audience=GUEST.audience_id)
    path = "Knowledge Base/Records/Work/Items/_summary.md"
    before = tool(store, "read_memory", who=GUEST, path=path)
    assert before["frontmatter"]["value"] == 1
    store.update_record(CID, item_key=OTHER, changes={"place": None}, why="correct private values",
        expected_item_version=receipts[-1]["after_item_hash"], expected_container_hash=receipts[-1]["after_container_hash"])
    assert tool(store, "read_memory", who=GUEST, path=path) == before
    scope.write_text(declaration + f"refs: [exomem://record/{CID}/{KEY}, exomem://record/{CID}/{OTHER}]\n")
    empty = tool(store, "read_memory", who=GUEST, path=path)
    assert empty["frontmatter"]["value"] == 0
    assert empty["frontmatter"]["completeness"] == "complete at basis"


@pytest.mark.parametrize("value", [
    {"metric": {"unreviewed_location": "12.345,64.987"}},
    {"metric": [{"unreviewed_location": "12.345,64.987"}]},
])
def test_nested_scalar_shape_changes_are_refused_before_commit(store, value):
    """A scalar property changed into a container that inherits ordinary metric admission."""
    from exomem.structured_collections import CollectionError

    receipts = create(store)
    before = query(store, {"select": ["count", "mixed"]})["rows"]
    with pytest.raises(CollectionError, match="SCHEMA_FIELD_TYPE"):
        store.update_record(CID, item_key=OTHER, changes={"mixed": value}, why="invalid nested shape",
            expected_item_version=receipts[-1]["after_item_hash"], expected_container_hash=receipts[-1]["after_container_hash"])
    assert query(store, {"select": ["count", "mixed"]})["rows"] == before


@pytest.mark.parametrize("summary", [False, True])
@pytest.mark.parametrize("declaration,valid,malformed", [
    ("{type: integer}", 3, {"unreviewed_location": "12.345,64.987"}),
    ("{type: array, items: {type: integer}}", [3], [{"unreviewed_location": "12.345,64.987"}]),
])
def test_old_stored_scalar_containers_are_unavailable_without_affecting_metrics(store, summary, declaration, valid, malformed):
    """An older stored malformed nested value that bypasses corrected write validation on JSON or typed reads."""
    import json

    from exomem.cli_ops import OpError
    from exomem.collection_store import typed_storage
    from exomem.structured_collections import CollectionError

    text = manifest_text().replace("    count: {type: integer}",
        "    count: {type: integer}\n    mixed: {type: object, properties: {metric: " + declaration + "}}")
    store.create_collection(manifest_path(), summary_text(text) if summary else text,
                            why="declare scalar coverage", scaffold=False)
    receipt = store.append_record(CID, item={"title": "One", "count": 7, "mixed": {"metric": valid}}, item_key=KEY, why="record")
    assert tool(store, "record_memory", who=GUEST, action="query", collection=CID,
                query={"version": 1, "select": ["mixed"]})["rows"] == [{"mixed": {"metric": valid}}]
    old = {"metric": malformed}
    with pytest.raises(CollectionError, match="SCHEMA_FIELD_TYPE"):
        store.update_record(CID, item_key=KEY, changes={"mixed": old}, why="invalid nested shape",
            expected_item_version=receipt["after_item_hash"], expected_container_hash=receipt["after_container_hash"])
    # Model data admitted by the older validator, retaining the live row identity and encoding.
    if summary:
        layout = typed_storage.require_layout(store.connection, CID)
        ordinal = layout.fields.index("mixed")
        store.connection.execute(f"UPDATE {layout.current_table} SET v{ordinal}=?", (json.dumps(old),))
    else:
        store.connection.execute("UPDATE items SET values_json=json_set(values_json,'$.mixed',json(?))", (json.dumps(old),))
    assert tool(store, "record_memory", who=GUEST, action="query", collection=CID,
                query={"version": 1, "select": ["count"]})["rows"] == [{"count": 7}]
    with pytest.raises(OpError) as error:
        tool(store, "record_memory", who=GUEST, action="query", collection=CID,
             query={"version": 1, "select": ["count", "mixed"]})
    assert "QUERY_UNAVAILABLE" in str(error.value) and "12.345" not in str(error.value)


def test_late_imported_scalar_container_is_rejected_after_the_preview_sample(store):
    """A row after the preview sample that turns a reviewed scalar source path into an unreviewed subtree."""
    from copy import deepcopy

    from test_collection_store_importer import CID as IMPORT_CID
    from test_collection_store_importer import (
        MAPPING,
        SOURCE,
        WORKOUT_FIELDS,
        call,
        ndjson,
        run,
        setup,
        start,
        status,
    )

    fields = WORKOUT_FIELDS + "    mixed: {type: object, properties: {metric: {type: integer}}}\n"
    mapping = deepcopy(MAPPING)
    mapping["fields"]["mixed"] = "mixed"
    mapping["coverage"]["mixed.metric"] = {"classification": None}
    rows = [{"id": str(n), "kind": "invented", "duration_s": 10, "metrics": {"calories": n, "distance_m": 1},
             "start": "2026-03-01T00:00:00+00:00", "mixed": {"metric": n}} for n in range(101)]
    rows[-1]["mixed"]["metric"] = {"new_private_location": "12.345,64.987"}
    setup(store, ndjson(rows), fields=fields)
    preview = call(store, OWNER, mode="preview", source_ref=SOURCE, format="ndjson", mapping=mapping)
    assert preview["rows"]["sampled"] == 100 and preview["rows"]["invalid"] == 0
    job = start(store, OWNER, mapping=mapping)
    run(store)
    result = status(store, job, OWNER)
    assert result["rows"]["imported"] == 100 and result["rows"]["rejected"] == 1
    response = tool(store, "record_memory", who=GUEST, action="query", collection=IMPORT_CID,
                    query={"version": 1, "select": ["mixed"], "page": {"limit": 200}})
    assert sorted(row["mixed"]["metric"] for row in response["rows"]) == list(range(100))


def test_schema_location_cannot_be_downgraded_by_leaf_coverage_or_extracted_alias(store):
    """Ordinary source-leaf coverage that overrides a classified parent and its differently named alias."""
    from copy import deepcopy

    from test_collection_store_importer import CID as IMPORT_CID
    from test_collection_store_importer import (
        MAPPING,
        WORKOUT_FIELDS,
        ndjson,
        run,
        setup,
        start,
        status,
    )

    fields = WORKOUT_FIELDS + (
        "    place: {type: object, classification: location, properties: {label: {type: string}}}\n"
        "    alias: {type: string}\n")
    mapping = deepcopy(MAPPING)
    mapping["fields"].update(place="place", alias="place.label")
    mapping["coverage"]["place.label"] = {"classification": None}
    setup(store, ndjson([{"id": "one", "kind": "invented", "duration_s": 10,
        "metrics": {"calories": 7, "distance_m": 1}, "start": "2026-03-01T00:00:00+00:00",
        "place": {"label": "private-schema-location"}}]), fields=fields)
    job = start(store, OWNER, mapping=mapping)
    run(store)
    assert status(store, job, OWNER)["rows"]["imported"] == 1
    result = tool(store, "record_memory", who=GUEST, action="query", collection=IMPORT_CID,
                  query={"version": 1})
    assert result["rows"][0]["calories"] == 7
    assert "place" not in result["rows"][0] and "alias" not in result["rows"][0]
    owner = tool(store, "record_memory", action="query", collection=IMPORT_CID,
                 query={"version": 1, "select": ["place", "alias"]})
    assert owner["rows"] == [{"place": {"label": "private-schema-location"}, "alias": "private-schema-location"}]


def test_nested_field_grant_does_not_release_a_literal_dotted_property_or_source_alias(store):
    """Flattened path identities that let one nested grant release a different property or mapped alias."""
    from copy import deepcopy

    from test_collection_store_importer import CID as IMPORT_CID
    from test_collection_store_importer import (
        MAPPING,
        WORKOUT_FIELDS,
        ndjson,
        run,
        setup,
        start,
        status,
    )

    from exomem.cli_ops import OpError

    initialize_vault_state_offline(store.root, source="structural field release fixture")
    fields = WORKOUT_FIELDS + (
        "    place: {type: object, classification: location, properties: {region: {type: object, "
        "properties: {label: {type: string}}}, 'region.label': {type: string}}}\n"
        "    alias: {type: string}\n")
    mapping = deepcopy(MAPPING)
    mapping["fields"].update(place="place", alias="place.region.label")
    mapping["coverage"]["place.region.label"] = {"classification": None}
    place = {"region": {"label": "released-region"}, "region.label": "withheld-exact-address"}
    setup(store, ndjson([{"id": "one", "kind": "invented", "duration_s": 10,
        "metrics": {"calories": 7, "distance_m": 1}, "start": "2026-03-01T00:00:00+00:00",
        "place": place}]), fields=fields)
    job = start(store, OWNER, mapping=mapping)
    run(store)
    assert status(store, job, OWNER)["rows"]["imported"] == 1
    basis = tool(store, "record_memory", action="inspect", collection=IMPORT_CID)["field_release_basis"]
    document = yaml.safe_load(release_document(basis))
    document["field_release"]["paths"] = [{"path": "place.region.label", "subtree": False}]
    proposal = govern(store, operation="propose", intent="Release only the nested region label",
        documents={"grants/nested-field.yaml": yaml.safe_dump(document)},
        selector_paths=[basis["path"]], target_ceiling=6, duration="standing")
    govern(store, operation="commit", proposal_id=proposal["proposal_id"])
    released = tool(store, "record_memory", who=GUEST, action="query", collection=IMPORT_CID,
                    query={"version": 1, "select": ["place"]})
    assert released["rows"] == [{"place": {"region": {"label": "released-region"}}}]
    with pytest.raises(OpError, match="QUERY_FIELD_UNAVAILABLE"):
        tool(store, "record_memory", who=GUEST, action="query", collection=IMPORT_CID,
             query={"version": 1, "select": ["alias"]})
    owner = tool(store, "record_memory", action="query", collection=IMPORT_CID,
                 query={"version": 1, "select": ["place", "alias"]})
    assert owner["rows"] == [{"place": place, "alias": "released-region"}]
