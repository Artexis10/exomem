"""Canonical collection authorization, exercised without projected files."""

import json
from dataclasses import replace

import pytest
from test_authorization_session_authority import NOW, _custody, _open, _seed
from test_collection_store_writer import CID, KEY, OTHER, create, manifest_path, manifest_text
from test_collection_store_writer import store as store
from test_governance_egress import _external, write_rule, write_scope

from exomem import structured_collections as collections
from exomem.collection_store.preview import preview_store
from exomem.governance import authorization_custody, policy, schema_v4, tool
from exomem.governance import store as authority_store
from exomem.governance.principal import RequestPrincipal, owner_principal, request_scope
from exomem.record_memory import record_memory


def withhold(store):
    write_scope(store.root, paths="Records/**")
    write_rule(store.root, ceiling=0)
    compiled = policy.load(store.root)
    assert not compiled.empty and not compiled.blocked


def test_withheld_append_refuses_without_changing_stored_data(store):
    """A hidden current collection cannot admit a new row or audit transition."""
    create(store)
    store.append_record(CID, item={"title": "One"}, item_key=KEY, why="capture")
    withhold(store)
    before = tuple(store.connection.iterdump())
    with request_scope(_external()):
        with pytest.raises(collections.CollectionError, match="COLLECTION_NOT_FOUND"):
            store.append_record(CID, item={"title": "Two"}, item_key=OTHER, why="capture")
    assert tuple(store.connection.iterdump()) == before


def test_withheld_request_replay_does_not_return_a_stored_receipt(store):
    """Transport retry cannot disclose a receipt after release was withdrawn."""
    create(store)
    arguments = dict(item={"title": "One"}, item_key=KEY, why="capture", request_id="retry")
    store.append_record(CID, **arguments)
    withhold(store)
    before = tuple(store.connection.iterdump())
    with request_scope(_external()):
        with pytest.raises(collections.CollectionError, match="COLLECTION_NOT_FOUND"):
            store.append_record(CID, **arguments)
    assert tuple(store.connection.iterdump()) == before


def test_withheld_preserved_source_refuses_before_storing_provenance(store):
    """An open collection must not admit provenance from a withheld file."""
    create(store)
    source = "Knowledge Base/Sources/private.md"
    page = store.root / source
    page.parent.mkdir(parents=True)
    page.write_text("---\ntype: source\n---\nPrivate evidence\n")
    write_scope(store.root, paths="Sources/**")
    write_rule(store.root, ceiling=0)
    before = tuple(store.connection.iterdump())
    with request_scope(_external()):
        with pytest.raises(collections.CollectionError, match="INVALID_RECORD_SOURCE"):
            store.append_record(CID, item={"title": "One"}, item_key=KEY,
                                sources=(source,), why="capture")
    assert tuple(store.connection.iterdump()) == before


def test_inspection_coverage_omits_withheld_observation_refs(store):
    """Observation coverage cannot disclose a restricted ancillary page."""
    from exomem import due_state

    create(store)
    manifest = store.root / manifest_path()
    manifest.parent.mkdir(parents=True)
    manifest.write_text(manifest_text())
    source = "Knowledge Base/Notes/private.md"
    page = store.root / source
    page.parent.mkdir(parents=True)
    page.write_text("---\ntype: insight\n---\nPrivate observation\n")
    ref = "exomem://memory/33333333-3333-4333-8333-333333333333"
    due_state.save(store.root, {
        "version": due_state.SCHEMA_VERSION,
        "claims": {manifest_path(): {"complete": True, "manifest_hash":
                    store.connection.execute("SELECT manifest_hash FROM collection_manifests").fetchone()[0]}},
        "categories": {"unreflected_observations": {manifest_path(): {"open": [{
            "component": {"family": "unreflected_observations", "kind": "backfill",
                          "collection": manifest_path(), "collection_id": CID,
                          "claims_signal": "sample-claims", "pages": [{"path": source, "ref": ref}]},
            "due_since": "2026-01-01",
        }]}}},
    })
    assert store.inspect_collection(CID)["coverage"]["unreflected_refs"] == [ref]
    write_scope(store.root, paths="Notes/**")
    write_rule(store.root, ceiling=0)
    with request_scope(_external()):
        result = store.inspect_collection(CID)
    assert result["coverage"]["unreflected"] == 0
    assert result["coverage"]["unreflected_refs"] == []
    assert ref not in str(result)


def test_inspection_omits_withheld_rows_before_decoding_payloads(store):
    """A single row restriction must remove its values, count and snapshot effect."""
    create(store)
    store.append_record(CID, item={"title": "One"}, item_key=KEY, why="capture")
    write_scope(store.root, paths=f"Records/**/{OTHER}.md")
    write_rule(store.root, ceiling=0)
    with request_scope(_external()):
        before = store.inspect_collection(CID)
    with request_scope(owner_principal()):
        store.append_record(CID, item={"title": "Hidden"}, item_key=OTHER, why="capture")
    with request_scope(_external()):
        after = store.inspect_collection(CID)
    assert after["coverage"]["committed"] == 1
    assert after["snapshot"] == before["snapshot"]
    assert after["projection"]["pending_views"] == before["projection"]["pending_views"]
    assert "Hidden" not in str(after)


@pytest.mark.parametrize("route", ["facade", "records", "planning", "adapter"])
def test_nested_query_cannot_reuse_another_principals_release(store, route):
    """A lower principal cannot inherit an owner's in-flight visible rows."""
    from exomem import planning, record_formats, record_governance
    from exomem.cli_ops import OpError

    profile = "planning" if route == "planning" else "records"
    create(store, profile)
    store.append_record(CID, item={"title": "Public"}, item_key=KEY, why="capture")
    store.append_record(CID, item={"title": "Owner secret"}, item_key=OTHER, why="capture")
    write_scope(store.root, paths=f"{profile.title()}/**/{OTHER}.md")
    write_rule(store.root, ceiling=0)
    before = tuple(store.connection.iterdump())
    with request_scope(owner_principal()), preview_store(store.root, store.handle) as writer, writer.read_collection(CID) as manifest:
        queries = {
            "facade": lambda: record_memory(store.root, "query", collection=CID),
            "records": lambda: record_governance.query_collection(store.root, manifest),
            "planning": lambda: planning.query(store.root, manifest),
            "adapter": record_formats.load_adapter(store.root, manifest).read,
        }
        query = queries[route]
        assert "Owner secret" in str(query())
        with request_scope(_external()):
            with pytest.raises((collections.CollectionError, OpError), match="COLLECTION_NOT_FOUND"):
                query()
        assert "Owner secret" in str(query())
    assert tuple(store.connection.iterdump()) == before


def test_copied_context_cannot_read_on_another_connection_thread(store):
    """Copying the request and preview binding cannot transfer SQLite ownership."""
    from concurrent.futures import ThreadPoolExecutor
    from contextvars import copy_context

    from exomem import record_formats
    from exomem.collection_store.connection import CollectionStoreError

    create(store)
    store.append_record(CID, item={"title": "Owner secret"}, item_key=KEY, why="capture")
    before = tuple(store.connection.iterdump())
    with request_scope(owner_principal()), preview_store(store.root, store.handle) as writer, writer.read_collection(CID) as manifest:
        adapter = record_formats.load_adapter(store.root, manifest)
        statements = []
        store.connection.set_trace_callback(statements.append)
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(copy_context().run, adapter.read)
                with pytest.raises(CollectionStoreError, match="COLLECTION_STORE_WRITER_THREAD"):
                    future.result()
        finally:
            store.connection.set_trace_callback(None)
        assert statements == []
        assert adapter.read().records[0].values["title"] == "Owner secret"
    assert tuple(store.connection.iterdump()) == before


@pytest.mark.parametrize("selector", ["path", "ref", "tag"])
def test_query_excludes_hidden_rows_from_counts_and_preserves_continuation(store, selector):
    third = "33333333-3333-4333-8333-333333333333"
    text = manifest_text().replace(
        "    count: {type: integer}\n",
        "    count: {type: integer}\n    tags: {type: array, items: {type: string}}\n",
    )
    store.create_collection(manifest_path(), text, why="create", scaffold=False)
    store.append_record(CID, item={"title": "One"}, item_key=KEY, why="capture")
    hidden = store.append_record(CID, item={"title": "Hidden", "tags": ["secret"]},
                                 item_key=OTHER, why="capture")
    final = store.append_record(CID, item={"title": "Three"}, item_key=third, why="capture")
    write_scope(store.root, paths=f"Records/**/{OTHER}.md" if selector == "path" else "Unrelated/**")
    scope = store.root / "Knowledge Base/_Governance/scopes/patterns.yaml"
    if selector == "ref":
        scope.write_text(scope.read_text() + f"refs: [exomem://record/{CID}/{OTHER}]\n")
    elif selector == "tag":
        scope.write_text(scope.read_text() + "tags: [secret]\n")
    write_rule(store.root, ceiling=0)
    with preview_store(store.root, store.handle), request_scope(_external()):
        first = record_memory(store.root, "query", collection=CID, limit=1, sort_by="title")
    assert first["total_matched"] == 2
    assert first["returned"] == 1
    assert first["continuation"]
    assert "Hidden" not in str(first)
    with request_scope(owner_principal()):
        store.update_record(CID, item_key=OTHER, changes={"title": "Still hidden"},
                            expected_container_hash=final["after_container_hash"],
                            expected_item_version=hidden["after_item_hash"], why="correct")
    with preview_store(store.root, store.handle), request_scope(_external()):
        second = record_memory(store.root, "query", collection=CID, limit=1,
                               sort_by="title", continuation=first["continuation"])
    assert second["snapshot"] == first["snapshot"]
    assert second["total_matched"] == 2
    assert second["rows"][0]["title"] == "Three"
    assert "hidden" not in str(second).lower()


def test_planning_hierarchy_omits_withheld_ancestor(store):
    from exomem.plan_memory import plan_memory

    create(store, "planning")
    store.append_record(CID, item={"title": "Parent", "kind": "outcome"},
                        item_key=KEY, why="capture")
    store.append_record(
        CID, item={"title": "Child", "kind": "initiative", "parent": f"exomem://plan/{CID}/{KEY}"},
        item_key=OTHER, why="capture",
    )
    write_scope(store.root, paths=f"Planning/**/{KEY}.md")
    write_rule(store.root, ceiling=0)
    with preview_store(store.root, store.handle), request_scope(_external()):
        result = plan_memory(
            store.root, "query", collection=CID, hierarchy_mode="ancestors",
            filters=[{"column": "plan_id", "op": "eq", "value": OTHER}],
        )
    assert result["total_matched"] == 1
    assert result["hierarchy"]["edges"] == []
    assert [node["plan_id"] for node in result["hierarchy"]["nodes"]] == [OTHER]


def test_saved_view_cannot_reveal_hidden_canonical_target_via_stale_projection(store):
    """Stale files cannot release a hidden or missing row, while a public row remains linkable."""
    text = manifest_text().replace("    count:", "    related: {type: link}\n    count:")
    text = text.replace("\n---\n", "\nviews:\n  hidden-target:\n    query:\n"
                        "      filters:\n        - column: related\n          op: eq\n"
                        f'          value: "[[Records/Work/Items/{KEY}]]"\n'
                        "      columns: [title, related]\n  permitted-target:\n    query:\n"
                        "      filters:\n        - column: related\n          op: eq\n"
                        f'          value: "[[Records/Work/Items/{OTHER}]]"\n'
                        "      columns: [title, related]\n---\n")
    store.create_collection(manifest_path(), text, why="create")
    store.append_record(CID, item={"title": "Hidden canonical target"}, item_key=KEY, why="capture")
    for key in (KEY, OTHER):
        projected = store.root / f"Knowledge Base/Records/Work/Items/{key}.md"
        projected.parent.mkdir(parents=True, exist_ok=True)
        projected.write_text("# Stale public-looking projection\n")
    write_scope(store.root, paths="Unrelated/**")
    scope = store.root / "Knowledge Base/_Governance/scopes/patterns.yaml"
    scope.write_text(scope.read_text() + f"refs: [exomem://record/{CID}/{KEY}]\n")
    write_rule(store.root, ceiling=0)
    with request_scope(_external()):
        result = store.inspect_collection(CID)
    assert result["coverage"]["committed"] == 0
    assert result["saved_views"] == []
    assert KEY not in str(result) and OTHER not in str(result)
    with request_scope(owner_principal()):
        store.append_record(CID, item={"title": "Public canonical target"}, item_key=OTHER, why="capture")
    with request_scope(_external()):
        result = store.inspect_collection(CID)
    assert result["coverage"]["committed"] == 1
    assert [view["name"] for view in result["saved_views"]] == ["permitted-target"]
    assert OTHER in str(result) and KEY not in str(result)


def test_absent_policy_does_not_make_excluded_row_changes_visible_in_snapshot(store):
    """The default-open shortcut must not expose a row hidden by access policy."""
    create(store)
    store.append_record(CID, item={"title": "One"}, item_key=KEY, why="capture")
    store.append_record(CID, item={"title": "Hidden"}, item_key=OTHER, why="capture")
    config = store.root / "Knowledge Base/_access.yaml"
    restriction = f"excluded: [Records/Work/Items/{OTHER}.md]\n"
    config.write_text(restriction)
    with request_scope(owner_principal()):
        before = store.inspect_collection(CID)
    config.write_text("excluded: []\n")
    owner_update(store, OTHER, "Hidden changed")
    config.write_text(restriction)
    with request_scope(owner_principal()):
        after = store.inspect_collection(CID)
    assert before["coverage"]["committed"] == after["coverage"]["committed"] == 1
    assert before["snapshot"] == after["snapshot"]
    assert "Hidden changed" not in str(after)


@pytest.mark.parametrize("layout,directory", [
    ("markdown-items", "Work"), ("markdown-items", "Work#current"), ("markdown-log", "Work#current"),
])
def test_literal_hash_paths_keep_template_and_collection_authority_separate(store, layout, directory):
    """Literal filenames must not inherit a manifest decision or lose path restrictions."""
    path = manifest_path().replace("Work/", directory + "/")
    source = "Items#current" if layout == "markdown-items" else "Log#current.md"
    text = manifest_text(layout=layout).replace("source: Items", f"source: {source}")
    text = text.replace("source: Log.md", f"source: {source}")
    if layout == "markdown-log":
        text = text.replace("  item: {level: 3, key: record_id}",
                            "  item_heading: {level: 3, fields: [{name: title, type: string}], separator: ' · '}\n"
                            "  child_rows: {prefix: '- ', delimiter: '|', fields: [field, value], container_field: details}\n"
                            "  insertion: newest-first")
        text = text.replace("    count: {type: integer}", "    details: {type: array, items: {type: object}}")
    template = path + "#private.md"
    text = text.replace("\n---\n", '\ntemplates: [{path: "_collection.md#private.md"}]\n---\n')
    store.create_collection(path, text, why="create")
    appended = store.append_record(CID, item={"title": "One"}, item_key=KEY, why="capture")
    page = store.root / template
    page.parent.mkdir(parents=True)
    page.write_text("# Private template\n")
    policy_path = f"Knowledge Base/Records/{directory}/{source}"
    if layout == "markdown-items":
        policy_path += f"/{KEY}.md"
    assert appended["affected_paths"] == [policy_path]
    replayed = store.append_record(CID, item={"title": "One"}, item_key=KEY, why="repeat")
    assert replayed["affected_paths"] == [policy_path]
    updated = store.update_record(CID, item_key=KEY, changes={"title": "Updated"},
                                  expected_container_hash=appended["after_container_hash"],
                                  expected_item_version=appended["after_item_hash"], why="correct")
    assert updated["affected_paths"] == [policy_path]
    with request_scope(_external()):
        result = store.inspect_collection(CID)
    assert result["coverage"]["committed"] == 1
    assert result["source_versions"][1]["path"] == policy_path
    assert "TEMPLATE_UNAVAILABLE" not in str(result)
    if layout == "markdown-log":
        ancillary = policy_path + "#private.md"
        (store.root / ancillary).write_text("# Ordinary ancillary file\n")
        with request_scope(_external()), store.handle.transaction(), store._authorization(mutation=False) as authorization:
            assert authorization.allows_file(f"{policy_path}#{KEY}")
            assert not authorization.allows_file(f"{policy_path}#{OTHER}")
            assert authorization.allows_file(ancillary)

    write_scope(store.root, paths=template.removeprefix("Knowledge Base/"))
    write_rule(store.root, ceiling=0)
    with request_scope(_external()):
        assert "TEMPLATE_UNAVAILABLE" in str(store.inspect_collection(CID))
    write_rule(store.root, ceiling=6)
    excluded = store.root / "Knowledge Base/_access.yaml"
    excluded.write_text(f'excluded: ["{template.removeprefix("Knowledge Base/")}"]\n')
    with request_scope(_external()):
        assert "TEMPLATE_UNAVAILABLE" in str(store.inspect_collection(CID))
    excluded.write_text(f'excluded: ["{policy_path.removeprefix("Knowledge Base/")}"]\n')
    with request_scope(_external()):
        assert store.inspect_collection(CID)["coverage"]["committed"] == 0
    excluded.write_text(f'excluded: ["{path.removeprefix("Knowledge Base/")}"]\n')
    with request_scope(_external()):
        with pytest.raises(collections.CollectionError, match="COLLECTION_NOT_FOUND"):
            store.inspect_collection(CID)


def test_discarded_held_saved_view_cannot_fall_back_to_stale_file_authority(store):
    """Discarding a held candidate must revoke its link even while its old view survives."""
    text = manifest_text().replace("    count:", "    related: {type: link}\n    count:")
    store.create_collection(manifest_path(), text, why="create")
    with pytest.raises(collections.CollectionError) as refused:
        store.append_record(CID, item={"title": "Candidate", "count": "bad"}, item_key=KEY, why="capture")
    held = refused.value.details["held"]
    page = store.root / held["path"]
    page.parent.mkdir(parents=True)
    page.write_bytes(store.connection.execute("SELECT held_bytes FROM held_candidates").fetchone()[0])
    text = text.replace("\n---\n", "\nviews:\n  held-target:\n    query:\n"
                        "      filters:\n        - column: related\n          op: eq\n"
                        f'          value: "[[{held["path"].removeprefix("Knowledge Base/").removesuffix(".md")}]]"\n'
                        "      columns: [title, related]\n---\n")
    guards = store.inspect_collection(CID)["lifecycle_guards"]
    store.revise_collection(CID, manifest_text=text, why="declare view", **guards)
    with request_scope(_external()):
        assert [view["name"] for view in store.inspect_collection(CID)["saved_views"]] == ["held-target"]
    store.discard_held(CID, held=held["held_id"], why="discard")
    assert page.exists()
    assert store.connection.execute("SELECT held_id FROM held_candidates").fetchall() == []
    assert store.connection.execute("SELECT path FROM projection_state WHERE kind='held'").fetchall() == []
    with request_scope(_external()):
        result = store.inspect_collection(CID)
    assert result["saved_views"] == []
    assert held["held_id"] not in str(result) and held["path"] not in str(result)


def test_proposed_row_metadata_cannot_enter_a_withheld_scope(store):
    """Authorization of the old row cannot authorize its newly restricted tags."""
    text = manifest_text().replace("    count:", "    tags: {type: array, items: {type: string}}\n    count:")
    store.create_collection(manifest_path(), text, why="create")
    first = store.append_record(CID, item={"title": "One", "tags": []}, item_key=KEY, why="capture")
    write_scope(store.root, paths="Unrelated/**")
    path = store.root / "Knowledge Base/_Governance/scopes/patterns.yaml"
    path.write_text(path.read_text() + "tags: [secret]\n")
    write_rule(store.root, ceiling=0)
    before = tuple(store.connection.iterdump())
    with request_scope(_external()):
        with pytest.raises(collections.CollectionError, match="COLLECTION_NOT_FOUND"):
            store.update_record(CID, item_key=KEY, changes={"tags": ["secret"]},
                                expected_container_hash=first["after_container_hash"],
                                expected_item_version=first["after_item_hash"], why="correct")
    assert tuple(store.connection.iterdump()) == before


def session(store, monkeypatch, *, paths="Records/**/Items/**"):
    write_scope(store.root, paths=paths)
    write_rule(store.root, ceiling=1, audience="principal:person-1")
    prospective = policy.compile_prospective(store.root, {})
    seed = _seed()
    seed = replace(seed, policy=replace(
        seed.policy, source_documents=prospective.target_documents,
        source_fingerprint=prospective.policy.fingerprint,
        compiled_policy=policy.canonical_compiled_bytes(prospective.policy),
        policy_fingerprint=prospective.policy.fingerprint,
        conflict_digest=prospective.snapshot.conflict_set_digest,
    ))
    conn = authority_store.open_connection(store.root)
    try:
        migration = schema_v4.migrate_v3_connection(conn, seed)
        custody = _custody(migration.activation_state_digest)
        opened = _open(conn, custody)
    finally:
        conn.close()
    monkeypatch.setattr("time.time", lambda: NOW + 2)
    monkeypatch.setattr(authorization_custody, "_load_authorization_custody_once", lambda *a, **k: custody)
    context = opened.context
    return RequestPrincipal(context.principal_id, surface="mcp", issuer_family=context.issuer_family,
                            verified_authorization_session=context)


def inspection_token(store, who):
    with request_scope(who):
        result = store.inspect_collection(CID)
    notices = result.get("governance", {}).get("notices", [{}])
    return notices[0].get("escalation_token")


def test_real_issue_redeem_consume_without_projected_file(store, monkeypatch):
    """A real L6 grant can release one canonical row without a Markdown view."""
    from exomem import reserved_paths

    create(store)
    store.append_record(CID, item={"title": "One"}, item_key=KEY, why="capture")
    who = session(store, monkeypatch)
    assert not (store.root / manifest_path()).exists()
    with preview_store(store.root, store.handle):
        token = inspection_token(store, who)
        assert isinstance(token, str)
        with request_scope(who), reserved_paths._owner_authority_scope("govern_memory"):
            result = tool.op_govern_memory(store.root, "grant", principal=who, token=token)
            assert result["status"] == "committed"
            inspection = store.inspect_collection(CID)
            assert inspection["coverage"]["committed"] == 1
            assert "One" in str(inspection)
    conn = authority_store.open_authorization_session_connection(store.root)
    try:
        assert conn.execute("SELECT ceiling FROM governance_session_grants").fetchone() == (6,)
        path, fingerprints = conn.execute("SELECT paths,fingerprints FROM governance_session_grants").fetchone()
        assert json.loads(path) == [f"exomem://record/{CID}/{KEY}"]
        assert json.loads(fingerprints)[0] != store.connection.execute("SELECT payload_hash FROM items").fetchone()[0]
    finally:
        conn.close()


def test_withheld_held_candidate_rolls_back_its_pending_view(store):
    """A validation failure cannot persist a candidate whose authored tags are withheld."""
    text = manifest_text().replace("    count:", "    tags: {type: array, items: {type: string}}\n    count:")
    store.create_collection(manifest_path(), text, why="create")
    write_scope(store.root, paths="Unrelated/**")
    path = store.root / "Knowledge Base/_Governance/scopes/patterns.yaml"
    path.write_text(path.read_text() + "tags: [secret]\n")
    write_rule(store.root, ceiling=0)
    before = tuple(store.connection.iterdump())
    with request_scope(_external()):
        with pytest.raises(collections.CollectionError, match="COLLECTION_NOT_FOUND"):
            store.append_record(CID, item={"title": "Candidate", "tags": ["secret"], "count": "bad"},
                                item_key=KEY, why="capture")
    assert tuple(store.connection.iterdump()) == before


@pytest.mark.parametrize("selector", ["paths", "refs", "projects", "tags", "types", "classes"])
def test_canonical_selectors_and_exclusions_share_the_file_kernel(store, selector):
    """Each selector can restrict a canonical row and its matching exclusion removes it."""
    text = manifest_text().replace("title: Work", "title: Work\nproject: Alpha\nprojects: [Beta]")
    text = text.replace("    count:", "    tags: {type: array, items: {type: string}}\n"
                                   "    classes: {type: array, items: {type: string}}\n    count:")
    store.create_collection(manifest_path(), text, why="create")
    item = {"title": "One", "tags": ["Secret"], "classes": ["Private"]}
    store.append_record(CID, item=item, item_key=KEY, why="capture")
    selectors = {"paths": [f"Records/**/{KEY}.md"], "refs": [f"exomem://record/{CID}/{KEY}"],
                 "projects": ["beta"], "tags": ["secret"], "types": ["record"], "classes": ["private"]}
    write_scope(store.root, paths="Unrelated/**")
    scope = store.root / "Knowledge Base/_Governance/scopes/patterns.yaml"
    scope.write_text(scope.read_text() + f"{selector}: {json.dumps(selectors[selector])}\n")
    write_rule(store.root, ceiling=0)
    with request_scope(_external()):
        with pytest.raises(collections.CollectionError, match="COLLECTION_NOT_FOUND"):
            store.append_record(CID, item=item, item_key=KEY, why="repeat")
    scope.write_text(scope.read_text() + f"exclude:\n  {selector}: {json.dumps(selectors[selector])}\n")
    with request_scope(_external()):
        assert store.append_record(CID, item=item, item_key=KEY, why="repeat")["outcome"] == "replayed"


def test_manifest_class_is_independent_of_row_classes(store):
    """Classifying the manifest cannot silently classify every row."""
    text = manifest_text().replace("title: Work", "title: Work\nclasses: [manifest-only]")
    store.create_collection(manifest_path(), text, why="create")
    store.append_record(CID, item={"title": "One"}, item_key=KEY, why="capture")
    write_scope(store.root, paths="Unrelated/**")
    scope = store.root / "Knowledge Base/_Governance/scopes/patterns.yaml"
    scope.write_text(scope.read_text() + "classes: [manifest-only]\n")
    write_rule(store.root, ceiling=0)
    with request_scope(_external()):
        with pytest.raises(collections.CollectionError, match="COLLECTION_NOT_FOUND"):
            store.inspect_collection(CID)
    scope.write_text(scope.read_text() + f"exclude:\n  refs: [{manifest_path()}]\n")
    with request_scope(_external()):
        assert store.inspect_collection(CID)["coverage"]["committed"] == 1


def redeem(store, who, token):
    from exomem import reserved_paths

    with request_scope(who), reserved_paths._owner_authority_scope("govern_memory"):
        return tool.op_govern_memory(store.root, "grant", principal=who, token=token)


def test_preserved_source_grant_keeps_its_file_hash_basis(store, monkeypatch):
    """A real file grant admits provenance only while its reviewed bytes survive."""
    import hashlib

    from exomem.governance import egress, membership
    from exomem.governance.decisions import decide

    create(store)
    source = "Knowledge Base/Sources/private.md"
    page = store.root / source
    page.parent.mkdir(parents=True)
    page.write_text("---\ntype: source\n---\nPrivate evidence\n")
    who = replace(session(store, monkeypatch, paths="Sources/**"), purpose="support")
    current = policy.load(store.root)
    decision = decide(membership.evaluate_path_only(store.root, source, current).require_classified(),
                      audience=who.audience_id, purpose="support", policy=current,
                      active_grants=current.grants)
    with preview_store(store.root, store.handle), request_scope(who):
        token = egress._mint_escalation_quietly(
            store.root, rel_path=source, who=who, purpose="support", decision=decision,
            requested_level=6, org_ceiling=egress._applicable_org_ceiling(current, decision),
            expected_content_hash=hashlib.sha256(page.read_bytes()).hexdigest(),
        )
        assert isinstance(token, str)
        redeem(store, who, token)
        store.append_record(CID, item={"title": "One"}, item_key=KEY, sources=(source,), why="capture")
        assert store.connection.execute("SELECT source_ref FROM item_sources").fetchall() == [(source,)]
        page.write_text(page.read_text() + "Changed evidence\n")
        before = tuple(store.connection.iterdump())
        with pytest.raises(collections.CollectionError, match="INVALID_RECORD_SOURCE"):
            store.append_record(CID, item={"title": "Two"}, item_key=OTHER, sources=(source,), why="capture")
        assert tuple(store.connection.iterdump()) == before


def owner_update(store, key, title):
    with request_scope(owner_principal()):
        guards = store.inspect_collection(CID)["lifecycle_guards"]
        version = store._version(store._item(CID, key))
        return store.update_record(CID, item_key=key, changes={"title": title},
                                   expected_container_hash=guards["expected_container_hash"],
                                   expected_item_version=version, why="correct")


def test_hidden_log_sibling_edit_preserves_token_and_consumed_grant(store, monkeypatch):
    """Shared log projection changes must not invalidate an unchanged row's authority."""
    text = manifest_text().replace("strategy: markdown-items\n  source: Items", "strategy: markdown-log\n  source: Log.md")
    text = text.replace("  format_version: 1\n", "  format_version: 1\n"
                        "  section: {level: 2, title: Entries}\n"
                        "  item_heading: {level: 3, fields: [{name: title, type: string}], separator: ' · '}\n"
                        "  child_rows: {prefix: '- ', delimiter: '|', fields: [field, value], container_field: details}\n"
                        "  insertion: newest-first\n")
    text = text.replace("    count: {type: integer}", "    details: {type: array, items: {type: object}}")
    store.create_collection(manifest_path(), text, why="create")
    store.append_record(CID, item={"title": "One", "details": []}, item_key=KEY, why="capture")
    store.append_record(CID, item={"title": "Two", "details": []}, item_key=OTHER, why="capture")
    who = session(store, monkeypatch, paths="Records/**/Log.md")
    with preview_store(store.root, store.handle):
        token = inspection_token(store, who)
        assert isinstance(token, str)
        owner_update(store, OTHER, "Hidden changed")
        assert redeem(store, who, token)["status"] == "committed"
        with request_scope(who):
            before = store.inspect_collection(CID)
        owner_update(store, OTHER, "Hidden changed again")
        with request_scope(who):
            after = store.inspect_collection(CID)
        assert after["coverage"]["committed"] == 1
        assert before["snapshot"] == after["snapshot"]
        assert "Hidden changed" not in str(after)


def test_changed_granted_row_invalidates_redemption_and_consumption(store, monkeypatch):
    """A token and an active grant each stop matching after their own row changes."""
    create(store)
    store.append_record(CID, item={"title": "One"}, item_key=KEY, why="capture")
    who = session(store, monkeypatch)
    with preview_store(store.root, store.handle):
        old_token = inspection_token(store, who)
        owner_update(store, KEY, "Two")
        with pytest.raises(tool.GovernanceError, match="AUTHORIZATION_SESSION_UNAVAILABLE"):
            redeem(store, who, old_token)
        assert redeem(store, who, inspection_token(store, who))["status"] == "committed"
        owner_update(store, KEY, "Three")
        with request_scope(who):
            assert store.inspect_collection(CID)["coverage"]["committed"] == 0


def test_old_content_grant_cannot_authorize_proposed_new_content(store, monkeypatch):
    """Current L6 granted authority cannot carry across an authored update."""
    create(store)
    first = store.append_record(CID, item={"title": "One"}, item_key=KEY, why="capture")
    who = session(store, monkeypatch)
    with preview_store(store.root, store.handle):
        redeem(store, who, inspection_token(store, who))
        before = tuple(store.connection.iterdump())
        with request_scope(who):
            with pytest.raises(collections.CollectionError, match="COLLECTION_NOT_FOUND"):
                store.update_record(CID, item_key=KEY, changes={"title": "Two"},
                                    expected_container_hash=first["after_container_hash"],
                                    expected_item_version=first["after_item_hash"], why="correct",
                                    request_id="old-content-grant")
        assert tuple(store.connection.iterdump()) == before


def test_another_session_cannot_redeem_or_consume_the_grant(store, monkeypatch):
    """A second verified conversation for the same principal does not share authority."""
    create(store)
    store.append_record(CID, item={"title": "One"}, item_key=KEY, why="capture")
    who = session(store, monkeypatch)
    conn = authority_store.open_authorization_session_connection(store.root)
    try:
        sibling = _open(conn, authorization_custody.load_authorization_custody(store.root, now=NOW + 2))
    finally:
        conn.close()
    other = replace(who, verified_authorization_session=sibling.context)
    with preview_store(store.root, store.handle):
        token = inspection_token(store, who)
        with pytest.raises(tool.GovernanceError, match="AUTHORIZATION_SESSION_UNAVAILABLE"):
            redeem(store, other, token)
        redeem(store, who, token)
        with request_scope(other):
            assert store.inspect_collection(CID)["coverage"]["committed"] == 0


@pytest.mark.parametrize("changed", ["purpose", "verified-session"])
def test_nested_query_rechecks_the_complete_principal(store, monkeypatch, changed):
    """The same audience cannot borrow a different purpose or verified session's grant."""
    from exomem import record_governance

    create(store)
    store.append_record(CID, item={"title": "Granted"}, item_key=KEY, why="capture")
    who = session(store, monkeypatch)
    other = (replace(who, purpose="different") if changed == "purpose"
             else replace(who, verified_authorization_session=None))
    with preview_store(store.root, store.handle) as writer:
        redeem(store, who, inspection_token(store, who))
        load = policy.load
        loads = []

        def counted_load(root):
            loads.append(root)
            return load(root)

        monkeypatch.setattr(policy, "load", counted_load)
        with request_scope(who), writer.read_collection(CID) as manifest:
            with request_scope(replace(who)):
                assert record_governance.query_collection(store.root, manifest).total_matched == 1
            with request_scope(other):
                with pytest.raises(collections.CollectionError, match="COLLECTION_NOT_FOUND"):
                    record_governance.query_collection(store.root, manifest)
            assert record_governance.query_collection(store.root, manifest).total_matched == 1
        assert loads == [store.root]


def test_ten_thousand_subjects_use_one_real_authority_catalog_read(store, monkeypatch):
    """An identity-only catalog cannot issue a grant lookup or decode payloads per row."""
    import uuid

    from exomem.collection_store import tokens

    create(store)
    encoded = json.dumps({"title": "Row"})
    with store.handle.transaction() as conn:
        conn.executemany(
            "INSERT INTO items(collection_id,item_key,row_version,schema_version,values_json,body,"
            "payload_hash,view_path,created_txn,updated_txn,governance_json) VALUES(?,?,1,1,?,'',?,?,1,1,?)",
            ((CID, str(uuid.UUID(int=index + 1)), encoded,
              tokens.payload_hash(1, str(uuid.UUID(int=index + 1)), {"title": "Row"}, ""),
              f"Knowledge Base/Records/Work/Items/{index}.md", '{"classes":[],"projects":[],"tags":[]}')
             for index in range(10000)),
        )
    who = session(store, monkeypatch)
    statements = []
    original = authority_store.open_authorization_session_connection

    def traced(*args, **kwargs):
        conn = original(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    loads = json.loads

    def identity_only(value, *args, **kwargs):
        assert value != encoded, "authorization decoded canonical values"
        return loads(value, *args, **kwargs)

    monkeypatch.setattr(authority_store, "open_authorization_session_connection", traced)
    monkeypatch.setattr(json, "loads", identity_only)
    with request_scope(who), store.handle.transaction(), store._authorization(mutation=False) as authorization:
        rows, _, _ = authorization.authorized_rows(CID)
        assert rows == []
        assert len(authorization.catalog(CID)) == 10001
    assert sum("FROM governance_session_grants WHERE authorization_session_id=" in sql for sql in statements) == 1
    assert statements.count("BEGIN") == 1


@pytest.mark.parametrize("metadata", [None, '{"projects":[],"tags":[],"classes":{}}'])
def test_missing_or_corrupt_identity_metadata_never_defaults_open(store, metadata):
    """Missing and malformed canonical metadata must refuse even with no policy."""
    create(store)
    store.append_record(CID, item={"title": "One"}, item_key=KEY, why="capture")
    with store.handle.transaction() as conn:
        conn.execute("UPDATE items SET governance_json=?", (metadata,))
    before = tuple(store.connection.iterdump())
    with pytest.raises(collections.CollectionError, match="COLLECTION_NOT_FOUND"):
        store.inspect_collection(CID)
    with pytest.raises(collections.CollectionError, match="COLLECTION_NOT_FOUND"):
        store.append_record(CID, item={"title": "Two"}, item_key=OTHER, why="capture")
    assert tuple(store.connection.iterdump()) == before


def test_blocked_policy_is_distinct_from_absent_policy(store):
    """A compile failure cannot take the default-open path used before enrollment."""
    create(store)
    write_scope(store.root, paths="Records/**")
    write_rule(store.root, ceiling=9)
    assert policy.load(store.root).blocked
    with request_scope(owner_principal()):
        with pytest.raises(collections.CollectionError, match="COLLECTION_NOT_FOUND"):
            store.inspect_collection(CID)


@pytest.mark.parametrize("tier", ["readonly", "excluded"])
def test_explicit_access_tier_binds_owner_mutations(store, tier):
    """Owner identity does not override explicit write or visibility protection."""
    create(store)
    (store.root / "Knowledge Base/_access.yaml").write_text(f"{tier}: [Records]\n")
    before = tuple(store.connection.iterdump())
    with request_scope(owner_principal()):
        with pytest.raises(collections.CollectionError, match="COLLECTION_NOT_FOUND"):
            store.append_record(CID, item={"title": "One"}, item_key=KEY, why="capture")
        if tier == "readonly":
            assert store.inspect_collection(CID)["coverage"]["committed"] == 0
        else:
            with pytest.raises(collections.CollectionError, match="COLLECTION_NOT_FOUND"):
                store.inspect_collection(CID)
    assert tuple(store.connection.iterdump()) == before


@pytest.mark.parametrize("contract", ["manifest", "type", "store"])
def test_changed_contract_cannot_redeem_or_consume_row_authority(store, monkeypatch, contract):
    """A row's store, manifest and type contract each belong to the grant basis."""
    import hashlib
    import uuid

    create(store)
    store.append_record(CID, item={"title": "One"}, item_key=KEY, why="capture")
    who = session(store, monkeypatch)
    with preview_store(store.root, store.handle):
        pending = inspection_token(store, who)
        redeem(store, who, inspection_token(store, who))
        if contract == "manifest":
            with request_scope(owner_principal()):
                guards = store.inspect_collection(CID)["lifecycle_guards"]
                store.revise_collection(CID, manifest_text=manifest_text().replace("title: Work", "title: Revised\nprojects: [Beta]"),
                                        why="revise", **guards)
        else:
            with store.handle.transaction() as conn:
                if contract == "store":
                    conn.execute("UPDATE store_meta SET value=? WHERE key='store_id'", (str(uuid.uuid4()),))
                else:
                    data = json.loads(conn.execute("SELECT declaration_json FROM collection_type_versions WHERE name='records' AND version=1").fetchone()[0])
                    data["version"] = 2
                    encoded = json.dumps(data)
                    conn.execute("INSERT INTO collection_type_versions VALUES('records',2,?,?,'builtin',1)",
                                 (encoded, hashlib.sha256(encoded.encode()).hexdigest()))
                    conn.execute("UPDATE collections SET type_version=2")
        with pytest.raises(tool.GovernanceError, match="AUTHORIZATION_SESSION_UNAVAILABLE"):
            redeem(store, who, pending)
        with request_scope(who), store.handle.transaction(), store._authorization(mutation=False) as authorization:
            rows, _, _ = authorization.authorized_rows(CID)
            assert rows == []


def test_create_request_identity_conflict_does_not_expose_withheld_receipt(store):
    """Reusing a request id at another path must authorize the stored collection first."""
    store.create_collection(manifest_path(), manifest_text(), why="create", request_id="create-retry")
    withhold(store)
    before = tuple(store.connection.iterdump())
    with request_scope(_external()):
        with pytest.raises(collections.CollectionError, match="COLLECTION_NOT_FOUND"):
            store.create_collection("Knowledge Base/Records/Other/_collection.md", manifest_text(),
                                    why="create", request_id="create-retry")
    assert tuple(store.connection.iterdump()) == before


def test_create_at_another_path_does_not_disclose_a_withheld_identity_conflict(store):
    """An alternate path cannot probe an existing withheld canonical collection id."""
    create(store)
    withhold(store)
    before = tuple(store.connection.iterdump())
    with request_scope(_external()):
        with pytest.raises(collections.CollectionError, match="COLLECTION_NOT_FOUND"):
            store.create_collection("Knowledge Base/Records/Other/_collection.md", manifest_text(), why="create")
    assert tuple(store.connection.iterdump()) == before


def test_v1_migration_backfills_canonical_metadata_and_restores_manifest_protection(tmp_path):
    """A shipped V1 store must acquire metadata from its own immutable manifest and row."""
    import sqlite3

    from exomem import vault
    from exomem.collection_store import schema, tokens, types

    conn = sqlite3.connect(tmp_path / "v1.sqlite", isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        schema._migrate_to_1(conn)
        conn.execute("INSERT INTO store_meta VALUES('schema_version','1')")
        types.register_builtins(conn, txn_id=1)
        text = manifest_text().replace("title: Work", "title: Work\nproject: Alpha\nclasses: [Manifest]")
        text = text.replace("    count:", "    tags: {type: array, items: {type: string}}\n"
                                       "    classes: {type: array, items: {type: string}}\n    count:")
        data, _, _ = vault.parse_frontmatter(text, strict=True)
        conn.execute("INSERT INTO collection_manifests VALUES(?,1,?,?,?,'[]',1)",
                     (CID, text, tokens.manifest_hash(text), json.dumps(data["item_schema"])))
        conn.execute("INSERT INTO collections(collection_id,type_name,type_version,manifest_path,source_path,layout,"
                     "manifest_version,generation,audit_reader_version,created_txn,updated_txn) "
                     "VALUES(?,'records',1,?,?,'markdown-items',1,1,1,1,1)",
                     (CID, manifest_path(), "Knowledge Base/Records/Work/Items"))
        values = {"title": "One", "tags": ["Secret"], "classes": ["Private"]}
        conn.execute("INSERT INTO items(collection_id,item_key,row_version,schema_version,values_json,body,payload_hash,"
                     "view_path,created_txn,updated_txn) VALUES(?,?,1,1,?,'',?,?,1,1)",
                     (CID, KEY, json.dumps(values), tokens.payload_hash(1, KEY, values, ""),
                      f"Knowledge Base/Records/Work/Items/{KEY}.md"))
        conn.execute("COMMIT")
        schema.ensure_schema(conn)
        assert json.loads(conn.execute("SELECT governance_json FROM collection_manifests").fetchone()[0]) == {
            "projects": ["alpha"], "tags": [], "classes": ["manifest"]}
        assert json.loads(conn.execute("SELECT governance_json FROM items").fetchone()[0]) == {
            "projects": ["alpha"], "tags": ["secret"], "classes": ["private"]}
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("UPDATE collection_manifests SET governance_json='{}'")
        assert schema.schema_version(conn) == 2
    finally:
        conn.close()


def test_manifest_grant_uses_its_own_basis_and_does_not_release_a_row(store, monkeypatch):
    """Manifest and row grants stay separate even when both have the same scope."""
    from exomem import due_state
    from exomem.collection_store import governance
    from exomem.governance import egress

    create(store)
    store.append_record(CID, item={"title": "One"}, item_key=KEY, why="capture")
    who = session(store, monkeypatch, paths="Records/**")
    with preview_store(store.root, store.handle), request_scope(who):
        with pytest.raises(collections.CollectionError, match="COLLECTION_NOT_FOUND"):
            store.inspect_collection(CID)
        store.connection.execute("BEGIN")
        try:
            with store._authorization(mutation=False) as authorization:
                manifest = authorization.catalog(CID)[0]
                current = governance.resolve_bound_membership(store.root, manifest_path(),
                    authorization.policy, authorization.logical_vault_id)
                decision = authorization.decision(manifest)
        finally:
            store.connection.execute("ROLLBACK")
        token = egress._mint_escalation_quietly(
            store.root, rel_path=current.path, who=who, purpose=authorization.purpose,
            decision=decision, requested_level=6,
            org_ceiling=egress._applicable_org_ceiling(authorization.policy, decision),
            expected_content_hash=current.fingerprint,
        )
        assert isinstance(token, str)
        redeem(store, who, token)
        projected = store.root / manifest_path()
        projected.parent.mkdir(parents=True)
        projected.write_text(manifest_text())
        due_state.save(store.root, {
            "version": due_state.SCHEMA_VERSION,
            "claims": {manifest_path(): {"complete": True, "manifest_hash": manifest.basis.manifest_hash}},
            "categories": {"unreflected_observations": {}},
        })
        inspection = store.inspect_collection(CID)
        assert inspection["coverage"]["committed"] == 0
        assert inspection["coverage"]["state"] == "complete"
        redeem(store, who, inspection_token(store, who))
        assert store.inspect_collection(CID)["coverage"]["committed"] == 1
