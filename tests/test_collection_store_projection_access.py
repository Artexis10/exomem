"""Projected bytes never replace canonical row release authority."""

import pytest
from test_collection_store_governance import inspection_token, redeem, session
from test_collection_store_writer import CID, KEY, OTHER, manifest_path, manifest_text
from test_collection_store_writer import store as store
from test_governance_egress import _external, _through_dispatcher, write_rule, write_scope

from exomem import structured_collections as collections
from exomem import vault
from exomem.collection_store.preview import preview_store
from exomem.governance import egress
from exomem.governance.principal import owner_principal, request_scope


def _log_manifest():
    text = manifest_text().replace("strategy: markdown-items\n  source: Items",
                                   "strategy: markdown-log\n  source: Log.md")
    text = text.replace("  format_version: 1\n", "  format_version: 1\n"
                        "  section: {title: Entries, level: 2}\n"
                        "  item_heading: {level: 3, fields: [{name: title, type: string}], separator: ' · '}\n"
                        "  child_rows: {prefix: '- ', delimiter: '|', fields: [field, value], container_field: details}\n"
                        "  insertion: newest-first\n")
    return text.replace("    count: {type: integer}", "    details: {type: array, items: {type: object}}")


@pytest.mark.parametrize("kind", ["item", "held"])
def test_projected_page_cannot_drop_canonical_restricted_tags(store, kind):
    # Editable item bytes and read-only Held views both depend on store authority.
    text = manifest_text().replace(
        "    count:", "    tags: {type: array, items: {type: string}}\n    count:"
    )
    store.create_collection(manifest_path(), text, why="create")
    item = {"title": "Restricted", "tags": ["secret"]}
    if kind == "held":
        item["count"] = "invalid"
        with pytest.raises(collections.CollectionError) as error:
            store.append_record(CID, item=item, item_key=KEY, why="capture")
        path = error.value.details["held"]["path"]
    else:
        receipt = store.append_record(CID, item=item, item_key=KEY, why="capture")
        path = receipt["affected_paths"][0]
        page = store.root / path
        frontmatter, body, _ = vault.parse_frontmatter(page.read_text(), strict=True)
        assert frontmatter["tags"] == ["secret"]
        frontmatter["tags"] = []
        page.write_text("---\n" + vault.serialize_frontmatter(frontmatter) + "\n---\n" + body)

    write_scope(store.root, paths="Unrelated/**")
    scope = store.root / "Knowledge Base/_Governance/scopes/patterns.yaml"
    scope.write_text(scope.read_text() + "tags: [secret]\n")
    write_rule(store.root, ceiling=0)

    with preview_store(store.root, store.handle), request_scope(_external()):
        with pytest.raises(ValueError, match="NOT_FOUND"):
            _through_dispatcher(store.root, "get", path=path)


def test_current_projection_is_returned_from_physical_bytes(store):
    store.create_collection(manifest_path(), manifest_text(), why="create")
    receipt = store.append_record(CID, item={"title": "Current"}, item_key=KEY,
                                  body="Canonical body\n", why="capture")
    path = receipt["affected_paths"][0]
    with preview_store(store.root, store.handle), request_scope(_external()):
        page = _through_dispatcher(store.root, "get", path=path, include_raw=True)
    assert page["content"] == (store.root / path).read_text()
    assert page["frontmatter"]["title"] == "Current"


@pytest.mark.parametrize("profile,command", [("records", "record_memory"), ("planning", "plan_memory")])
def test_canonical_inspection_versions_do_not_depend_on_unaccepted_view_edits(store, profile, command):
    """A view edit cannot remove committed row identity from structured inspection."""
    store.create_collection(manifest_path(profile), manifest_text(profile), why="create")
    receipt = store.append_record(CID, item={"title": "Current"}, item_key=KEY, why="capture")
    path = receipt["affected_paths"][0]
    with preview_store(store.root, store.handle), request_scope(owner_principal()):
        before = _through_dispatcher(store.root, command, action="inspect", collection=CID)
        assert path in {version["path"] for version in before["source_versions"]}
        page = store.root / path
        page.write_text(page.read_text() + "\nUnaccepted local edit\n")
        after = _through_dispatcher(store.root, command, action="inspect", collection=CID)
        assert after["source_versions"] == before["source_versions"]
        assert after["snapshot"] == before["snapshot"]
        assert "Unaccepted local edit" not in str(after)
        with pytest.raises(ValueError, match="NOT_FOUND"):
            _through_dispatcher(store.root, "get", path=path, include_raw=True)


def test_mcp_second_pass_keeps_provenance_but_json_copy_has_no_exemption(store):
    """The real adapter retains the carrier; wire JSON cannot recreate it."""
    import json

    from exomem import command_surface, commands

    store.create_collection(manifest_path(), manifest_text(), why="create")
    first = store.append_record(CID, item={"title": "One"}, item_key=KEY, why="capture")
    path = first["affected_paths"][0]
    page = store.root / path
    page.write_text(page.read_text() + "\nUnaccepted local edit\n")
    command = next(command for command in commands.COMMANDS if command.name == "record_memory")
    bound = command_surface.bind_vault(command.leaf, store.root, command=command)
    with preview_store(store.root, store.handle), request_scope(owner_principal()):
        result = bound(action="inspect", collection=CID)
        assert path in {entry["path"] for entry in result["source_versions"]}
        clone = json.loads(json.dumps(result))
        filtered = egress.postfilter("record_memory", clone, store.root)
        assert path not in {entry["path"] for entry in filtered["source_versions"]}


def test_canonical_metadata_does_not_authorize_the_same_resource_reference(store):
    """Canonical row metadata cannot release an authored reference to edited view bytes."""
    path = f"Knowledge Base/Records/Work/Items/{KEY}.md"
    text = manifest_text().replace("    count:", "    resource: {type: string}\n    count:")
    store.create_collection(manifest_path(), text, why="create")
    store.append_record(CID, item={"title": "Current", "resource": f"Read [[{path}]] next."},
                        item_key=KEY, why="capture")
    page = store.root / path
    page.write_text(page.read_text() + "\nUnaccepted local edit\n")
    with preview_store(store.root, store.handle), request_scope(owner_principal()):
        result = _through_dispatcher(store.root, "record_memory", action="inspect", collection=CID)
    assert path in {entry["path"] for entry in result["source_versions"]}
    value = result["observed_values"]["resource"]["values"][0]["value"]
    assert path not in value
    assert egress.WITHHELD_REFERENCE in value


def test_admitted_source_versions_still_scrub_and_filter_ordinary_copies(store):
    """Admitted metadata is not a credential bypass or authority for another occurrence."""
    from test_governance_egress import _receipt_records
    from test_governance_postfilter import AWS_KEY

    from exomem.collection_store import governance
    from exomem.governance import scrubber

    store.create_collection(manifest_path(), manifest_text(), why="create")
    receipt = store.append_record(CID, item={"title": "Current"}, item_key=KEY, why="capture")
    path = receipt["affected_paths"][0]
    page = store.root / path
    page.write_text(page.read_text() + "\nUnaccepted local edit\n")
    with preview_store(store.root, store.handle), request_scope(owner_principal()):
        result = _through_dispatcher(store.root, "record_memory", action="inspect", collection=CID)
        version = next(entry for entry in result["source_versions"] if entry["path"] == path)
        version["hash"] = AWS_KEY
        result["ordinary_versions"] = [dict(version)]
        # Model a trusted producer's metadata, not an unsealed public mutation.
        result = governance._seal_inspection_projection(result, governance._inspection_evidence(result))
        with egress.disclosure_boundary(store.root, "record_memory") as collector:
            cleaned = egress.postfilter("record_memory", result, store.root)
            egress.emit_boundary_receipt(collector)
        assert next(entry for entry in cleaned["source_versions"] if entry["path"] == path)["hash"] == scrubber.NOTICE
        assert cleaned["ordinary_versions"] == []
        assert egress.postfilter("record_memory", cleaned, store.root) == cleaned
    records = _receipt_records(store.root)
    assert [record["event_type"] for record in records[-2:]] == ["credential_block", "disclosure"]
    assert any(outcome["decision"] == "withheld" for outcome in records[-1]["outcomes"])
    assert AWS_KEY not in str(records)


def test_source_version_extra_fields_keep_generic_filtering_and_scrub_collisions(store):
    """An admitted path cannot exempt nested authored content in an extended version row."""
    from test_governance_postfilter import AWS_KEY

    from exomem.collection_store import governance
    from exomem.governance import scrubber

    store.create_collection(manifest_path(), manifest_text(), why="create")
    receipt = store.append_record(CID, item={"title": "Current"}, item_key=KEY, why="capture")
    path = receipt["affected_paths"][0]
    page = store.root / path
    page.write_text(page.read_text() + "\nUnaccepted local edit\n")
    with preview_store(store.root, store.handle), request_scope(owner_principal()):
        result = _through_dispatcher(store.root, "record_memory", action="inspect", collection=CID)
        version = next(entry for entry in result["source_versions"] if entry["path"] == path)
        version["details"] = {"entries": [{"path": path}], "ReSoUrCe_text": f"Read [[{path}]] next.",
                              AWS_KEY: "scrubbed key", scrubber.NOTICE: "authored key"}
        result = governance._seal_inspection_projection(result, governance._inspection_evidence(result))
        cleaned = egress.postfilter("record_memory", result, store.root)
        details = next(entry for entry in cleaned["source_versions"] if entry["path"] == path)["details"]
        assert details == {"entries": [], "ReSoUrCe_text": f"Read {egress.WITHHELD_REFERENCE} next.",
                           scrubber.NOTICE: "authored key", f"{scrubber.NOTICE}#1": "scrubbed key"}
        assert egress.postfilter("record_memory", cleaned, store.root) == cleaned


@pytest.mark.parametrize("change", ["row", "release", "payload"])
def test_terminal_inspection_refuses_a_changed_basis_or_payload(store, change):
    """Old counts/values cannot survive a later mutation or loss of authority."""
    store.create_collection(manifest_path(), manifest_text(), why="create")
    first = store.append_record(CID, item={"title": "One"}, item_key=KEY, why="capture")
    with preview_store(store.root, store.handle), request_scope(_external()):
        result = _through_dispatcher(store.root, "record_memory", action="inspect", collection=CID)
        if change == "row":
            store.update_record(CID, item_key=KEY, changes={"title": "Two"},
                                expected_container_hash=first["after_container_hash"],
                                expected_item_version=first["after_item_hash"], why="correct")
        elif change == "release":
            write_scope(store.root, paths="Records/**/Items/**")
            write_rule(store.root, ceiling=0)
        else:
            result["body"] = "Untrusted addition"
        with pytest.raises(collections.CollectionError, match="COLLECTION_NOT_FOUND"):
            egress.postfilter("record_memory", result, store.root)


def test_partial_log_inspection_preserves_visible_metadata_across_hidden_edit(store):
    """A shared log name is not a grant to read its withheld sibling's bytes."""
    store.create_collection(manifest_path(), _log_manifest(), why="create")
    store.append_record(CID, item={"title": "Visible"}, item_key=KEY, why="capture")
    hidden = store.append_record(CID, item={"title": "Hidden"}, item_key=OTHER, why="capture")
    write_scope(store.root, paths="Unrelated/**")
    scope = store.root / "Knowledge Base/_Governance/scopes/patterns.yaml"
    scope.write_text(scope.read_text() + f"refs: [exomem://record/{CID}/{OTHER}]\n")
    write_rule(store.root, ceiling=0)
    with preview_store(store.root, store.handle), request_scope(_external()):
        result = _through_dispatcher(store.root, "record_memory", action="inspect", collection=CID)
        assert result["coverage"]["committed"] == 1
        path = "Knowledge Base/Records/Work/Log.md"
        assert path in {entry["path"] for entry in result["source_versions"]}
        assert "Hidden" not in str(result)
        with request_scope(owner_principal()):
            store.update_record(CID, item_key=OTHER, changes={"title": "Changed hidden"},
                                expected_container_hash=hidden["after_container_hash"],
                                expected_item_version=hidden["after_item_hash"], why="correct")
        assert egress.postfilter("record_memory", result, store.root) == result
        assert egress.release_level_for_path_only(store.root, path) == 0


def test_terminal_filter_rechecks_distinct_views_after_bytes_and_policy_change(store):
    """Sharing a terminal read cannot share the first path's verdict or old authority."""
    store.create_collection(manifest_path(), manifest_text(), why="create")
    receipts = [
        store.append_record(CID, item={"title": title}, item_key=key, why="capture")
        for title, key in (("One", KEY), ("Two", OTHER))
    ]
    paths = [receipt["affected_paths"][0] for receipt in receipts]
    payload = {"items": [{"path": path} for path in paths]}
    with preview_store(store.root, store.handle), request_scope(_external()):
        assert egress.filter_withheld_entries(store.root, payload) == payload
        page = store.root / paths[0]
        page.write_text(page.read_text() + "\nUncommitted view edit\n")
        write_scope(store.root, paths=paths[1].removeprefix("Knowledge Base/"))
        write_rule(store.root, ceiling=0)
        assert egress.filter_withheld_entries(store.root, payload) == {"items": []}


def test_terminal_filter_observes_a_new_deletion_tombstone(store):
    """A fresh response cannot reuse the preceding response's live-path verdict."""
    from exomem.governance import lifecycle

    store.create_collection(manifest_path(), manifest_text(), why="create")
    receipt = store.append_record(CID, item={"title": "Current"}, item_key=KEY, why="capture")
    path = receipt["affected_paths"][0]
    write_scope(store.root, paths=path.removeprefix("Knowledge Base/"))
    write_rule(store.root, ceiling=0)
    payload = {"items": [{"path": path}]}
    with preview_store(store.root, store.handle), request_scope(owner_principal()):
        assert egress.filter_withheld_entries(store.root, payload) == payload
        operation = lifecycle.begin_deletion(
            store.root, source_rel=path, trash_rel="Knowledge Base/_trash/current.md",
        )
        assert operation.governed
        assert egress.filter_withheld_entries(store.root, payload) == {"items": []}


@pytest.mark.parametrize("stale", [False, True], ids=["edited-current", "old-version"])
def test_current_row_grant_cannot_release_unproven_projection_bytes(store, monkeypatch, stale):
    store.create_collection(manifest_path(), manifest_text(), why="create")
    first = store.append_record(CID, item={"title": "One"}, item_key=KEY, why="capture")
    path = first["affected_paths"][0]
    page = store.root / path
    original = page.read_bytes()
    if stale:
        store.update_record(CID, item_key=KEY, changes={"title": "Two"},
                            expected_container_hash=first["after_container_hash"],
                            expected_item_version=first["after_item_hash"], why="correct")
    who = session(store, monkeypatch)
    with preview_store(store.root, store.handle):
        token = inspection_token(store, who)
        assert redeem(store, who, token)["status"] == "committed"
        with request_scope(who):
            assert _through_dispatcher(store.root, "get", path=path)["frontmatter"]["title"] == (
                "Two" if stale else "One"
            )
            page.write_bytes(original if stale else original.replace(b"title: One", b"title: Forged"))
            with pytest.raises(ValueError, match="NOT_FOUND"):
                _through_dispatcher(store.root, "get", path=path)


@pytest.mark.parametrize("level", [1, 5])
def test_current_projection_preserves_partial_release_levels(store, level):
    store.create_collection(manifest_path(), manifest_text(), why="create")
    receipt = store.append_record(CID, item={"title": "Private title"}, item_key=KEY,
                                  body="Permitted excerpt\n", why="capture")
    path = receipt["affected_paths"][0]
    write_scope(store.root, paths="Records/**/Items/**")
    write_rule(store.root, ceiling=level, extra='options:\n  notice: "Limited"\n' if level == 1 else "")
    with preview_store(store.root, store.handle), request_scope(_external()):
        result = _through_dispatcher(store.root, "get", path=path)
    assert "Private title" not in str(result)
    if level == 1:
        assert result["level"] == 1
        assert result["notice"] == "Limited"
        assert "path" not in result
    else:
        assert result["release_level"] == 5
        assert "Permitted excerpt" in result["body"]
        assert "frontmatter" not in result


@pytest.mark.parametrize("kind", ["unknown", "retired", "log"])
def test_owned_unproven_projection_is_absent_with_empty_policy(store, kind):
    text = _log_manifest() if kind == "log" else manifest_text()
    store.create_collection(manifest_path(), text, why="create")
    if kind == "log":
        path = "Knowledge Base/Records/Work/Log.md"
    elif kind == "unknown":
        path = "Knowledge Base/Records/Work/Items/unbound.md"
    else:
        receipt = store.append_record(CID, item={"title": "One"}, item_key=KEY, why="capture")
        path = receipt["affected_paths"][0]
        store.connection.execute("UPDATE items SET view_path=? WHERE item_key=?",
                                 (path.replace(KEY, "moved"), KEY))
    page = store.root / path
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("---\ntype: record\ntitle: Unproven\n---\nSecret body\n")
    with preview_store(store.root, store.handle), request_scope(_external()):
        with pytest.raises(ValueError, match="NOT_FOUND"):
            _through_dispatcher(store.root, "get", path=path)
        if kind != "log":
            assert egress.release_level_for_path_only(store.root, path) == 0
        keep = egress.restricted_release_filter(store.root)
        assert keep is not None and not keep(path)


def test_whole_log_path_requires_every_canonical_row(store):
    import yaml
    from conftest import initialize_vault_state_offline
    from test_collection_field_admission import GUEST, govern, release_document, tool

    initialize_vault_state_offline(store.root, source="log field-release fixture")
    text = _log_manifest().replace("details: {type: array,", "details: {type: array, classification: location,")
    store.create_collection(manifest_path(), text, why="declare reviewed log details")
    store.append_record(CID, item={"title": "Visible"}, item_key=KEY, why="capture")
    store.append_record(CID, item={"title": "Hidden"}, item_key=OTHER, why="capture")
    path = "Knowledge Base/Records/Work/Log.md"
    write_scope(store.root, paths="Unrelated/**")
    scope = store.root / "Knowledge Base/_Governance/scopes/patterns.yaml"
    scope.write_text(scope.read_text() + f"refs: [exomem://record/{CID}/{OTHER}]\n")
    write_rule(store.root, ceiling=0, audience=GUEST.audience_id)
    with preview_store(store.root, store.handle), request_scope(GUEST):
        assert egress.release_level_for_path_only(store.root, path) == 0
        assert egress.release_level_for_path_only(store.root, f"{path}#{KEY}") == 0
    basis = tool(store, "record_memory", action="inspect", collection=CID)["field_release_basis"]
    document = yaml.safe_load(release_document(basis, GUEST))
    document["field_release"]["paths"] = [{"path": "details", "subtree": True}]
    proposal = govern(store, operation="propose", intent="Release reviewed log details",
                      documents={"grants/log-details.yaml": yaml.safe_dump(document)},
                      selector_paths=[basis["path"]], target_ceiling=6, duration="standing")
    govern(store, operation="commit", proposal_id=proposal["proposal_id"])
    with preview_store(store.root, store.handle), request_scope(GUEST):
        assert egress.release_level_for_path_only(store.root, path) == 0
        assert egress.release_level_for_path_only(store.root, f"{path}#{KEY}") == 6
        assert egress.release_level_for_path_only(store.root, f"{path}#{OTHER}") == 0


def test_owner_walk_treats_an_unbound_projection_directory_as_absent(store):
    store.create_collection(manifest_path(), manifest_text(), why="create")
    directory = "Knowledge Base/Records/Work/Items"
    page = store.root / directory / "unbound.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("# Unproven content\n")
    with preview_store(store.root, store.handle), request_scope(owner_principal()):
        with pytest.raises(ValueError, match="NOT_FOUND"):
            _through_dispatcher(store.root, "list_directory", path=directory)
        assert _through_dispatcher(store.root, "suggest_relations") == {
            "available": False, "reason": "audience_restricted",
        }


def test_owner_walk_on_an_empty_policy_decides_only_marker_owned_paths(tmp_path, monkeypatch):
    import json

    from exomem import find_corpus
    from exomem.collection_store import authority

    root = tmp_path / "vault"
    ordinary = "Knowledge Base/Notes/plain.md"
    owned = "Knowledge Base/Records/Work/Items/item.md"
    for path in (ordinary, owned):
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text("---\ntype: insight\nstatus: active\n---\n\nBody.\n")
    sid = "11111111-1111-4111-8111-111111111111"
    marker = authority.marker_path(root)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({
        "version": 2, "mode": "store", "default_authority": "file", "store_id": sid,
        "authority_epoch": 1,
        "collections": [{"collection_id": CID, "manifest_path": "Knowledge Base/Records/Work/_collection.md",
                         "authority": "store", "store_id": sid,
                         "source_path": "Knowledge Base/Records/Work/Items", "layout": "markdown-items"}],
        "collection_store_fence": {"capability": "collections-store-v1", "generation": 1},
    }))
    # A walk over ordinary files keeps the empty-policy answer and parses nothing;
    # parsing each page cost the owner's carry rarity seconds per activation.
    monkeypatch.setattr(find_corpus, "parse_page", lambda *a, **kw: pytest.fail("parsed an ordinary page"))
    with request_scope(owner_principal()):
        keep = egress.release_walk_filter(root)
        assert keep(ordinary)
        # A marker-owned path still needs the store, which no service serves here.
        assert not keep(owned)
