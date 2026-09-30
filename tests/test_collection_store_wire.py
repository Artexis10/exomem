"""File/store facade goldens with only the OpenSpec-ruled normalizations."""

import hashlib
import json

import pytest

from exomem import mutation_terminal, records, structured_collections as collections
from exomem.cli_ops import OpError
from exomem.collection_store import connection
from exomem.plan_memory import plan_memory
from exomem.record_memory import record_memory
from test_collection_store_writer import CID, KEY, OTHER, manifest_path, manifest_text


def normalized(value):
    hashes = {
        "before_item_hash", "after_item_hash", "before_container_hash", "after_container_hash",
        "snapshot", "expected_container_hash", "expected_manifest_hash",
        "before_manifest_hash", "after_manifest_hash",
    }
    identities = {"audit_correlation", "transition_id", "held_at", "committed_at"}
    if isinstance(value, dict):
        lifecycle = value.get("operation") == "revise"
        source_version = set(value) == {"path", "hash"}
        # A saved view's identity hashes its manifest hash, so it is mode-local (tasks.md P1a.5).
        saved_view = set(value) == {"name", "definition", "identity"}
        return {
            k: "<hash>" if v is not None and (
                k in hashes or (k == "payload_hash" and lifecycle) or (k == "hash" and source_version)
            ) else "<identity>" if v is not None and (
                k in identities or (k == "identity" and saved_view)
            ) else normalized(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [normalized(v) for v in value]
    return value


def same_wire(left, right):
    assert json.dumps(normalized(left), sort_keys=True) == json.dumps(normalized(right), sort_keys=True)


@pytest.fixture
def paired(tmp_path, monkeypatch):
    from exomem.collection_store.preview import preview_store

    monkeypatch.setenv("EXOMEM_COLLECTION_STORE_PREVIEW", "1")
    monkeypatch.setattr("exomem.records._capture_sweep_carrier", lambda *a, **kw: None)
    monkeypatch.setattr("exomem.records._due_state_carrier", lambda *a, **kw: None)
    roots = [tmp_path / "files", tmp_path / "store"]
    for root in roots:
        (root / "Knowledge Base").mkdir(parents=True)
        (root / "Knowledge Base/log.md").write_text("# Log\n")
    with connection.open_writer(tmp_path / "collections.sqlite", lease_check=lambda: True) as handle:
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
    for manifest, inspection in zip(manifests, inspections):
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
