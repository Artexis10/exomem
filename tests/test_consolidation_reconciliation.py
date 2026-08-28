from __future__ import annotations

import hashlib
import itertools
from dataclasses import replace
from pathlib import Path

import pytest

from exomem.governance import consolidation_reconciliation as reconciliation


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _object(
    ref: str,
    path: str,
    content: str,
    *,
    identity: str | None = None,
    logical: str | None = None,
    kind: str = "content",
    append_only: bool = False,
    lifecycle: str | None = None,
    dependencies: tuple[reconciliation.ObjectDependency, ...] = (),
    anchors: tuple[str, ...] = (),
) -> reconciliation.InventoryObject:
    content_digest = _digest(content)
    return reconciliation.InventoryObject(
        object_ref=ref,
        path=path,
        entry_type="file",
        size=len(content.encode("utf-8")),
        sha256=content_digest,
        bundle_sha256=content_digest,
        durable_identity=identity,
        logical_content_sha256=_digest(logical) if logical is not None else None,
        object_kind=kind,
        append_only=append_only,
        lifecycle=lifecycle,
        anchors=anchors,
        dependencies=dependencies,
    )


def _inventory(
    role: str, *objects: reconciliation.InventoryObject
) -> reconciliation.ReconciliationInventory:
    return reconciliation.build_inventory(
        tuple(objects), role=role, snapshot_digest=_digest(f"{role}-snapshot")
    )


def _class_fixture(
    primary_class: str, *, append_only: bool = False
) -> tuple[
    reconciliation.ReconciliationInventory,
    reconciliation.ReconciliationInventory,
]:
    if primary_class == "C4":
        source = _object(
            "source",
            "Knowledge Base/Notes/source.md",
            "source",
            identity="source-id",
            logical="same-logical",
        )
        destination = _object(
            "destination",
            "Knowledge Base/Notes/destination.md",
            "destination",
            identity="destination-id",
            logical="same-logical",
        )
    elif primary_class == "C5":
        source = _object(
            "source",
            "Knowledge Base/Sources/shared.md" if append_only else "Knowledge Base/Notes/shared.md",
            "source",
            kind="source" if append_only else "content",
            append_only=append_only,
        )
        destination = _object(
            "destination",
            source.path,
            "destination",
            kind=source.object_kind,
            append_only=append_only,
        )
    elif primary_class == "C6":
        source = _object(
            "source",
            "Knowledge Base/Notes/source.md",
            "source",
            identity="same-id",
        )
        destination = _object(
            "destination",
            "Knowledge Base/Notes/destination.md",
            "destination",
            identity="same-id",
        )
    elif primary_class == "C7":
        source = _object(
            "source",
            "Knowledge Base/Notes/source.md",
            "source",
            dependencies=(
                reconciliation.ObjectDependency(
                    dependency_ref="missing",
                    dependency_kind="wikilink",
                    target_path="Knowledge Base/Notes/missing.md",
                ),
            ),
        )
        destination = None
    elif primary_class == "C8":
        source = _object(
            "source",
            "Knowledge Base/_Governance/rules/source.yaml",
            "rules: []",
            kind="policy",
        )
        destination = None
    else:  # pragma: no cover - test helper is closed by callers
        raise AssertionError(primary_class)
    return (
        _inventory("source", source),
        _inventory("destination", *(() if destination is None else (destination,))),
    )


def test_golden_fixture_classifies_every_c1_c8_class_with_exact_precedence() -> None:
    destination = _inventory(
        "destination",
        _object("d-c1", "Knowledge Base/Notes/exact.md", "same"),
        _object("d-c3", "Knowledge Base/Notes/moved.md", "moved", identity="id-c3"),
        _object(
            "d-c4",
            "Knowledge Base/Notes/logical-destination.md",
            "---\nexomem_id: destination\n---\nbody",
            identity="id-c4-d",
            logical="body",
        ),
        _object("d-c5", "Knowledge Base/Notes/path.md", "destination-path"),
        _object(
            "d-c6",
            "Knowledge Base/Notes/identity-destination.md",
            "destination-identity",
            identity="id-c6",
        ),
        _object("d-dependency", "Knowledge Base/Notes/dependency.md", "dependency"),
        _object("d-c8-overlap", "Knowledge Base/Notes/authority-exact.md", "same"),
    )
    missing = reconciliation.ObjectDependency(
        dependency_ref="dep-missing",
        dependency_kind="wikilink",
        target_path="Knowledge Base/Notes/missing.md",
    )
    source = _inventory(
        "source",
        _object("s-c1", "Knowledge Base/Notes/exact.md", "same"),
        _object("s-c2", "Knowledge Base/Notes/new.md", "new"),
        _object("s-c3", "Knowledge Base/Notes/old.md", "moved", identity="id-c3"),
        _object(
            "s-c4",
            "Knowledge Base/Notes/logical-source.md",
            "---\nexomem_id: source\n---\nbody",
            identity="id-c4-s",
            logical="body",
        ),
        _object("s-c5", "Knowledge Base/Notes/path.md", "source-path"),
        _object(
            "s-c6",
            "Knowledge Base/Notes/identity-source.md",
            "source-identity",
            identity="id-c6",
        ),
        _object(
            "s-c7",
            "Knowledge Base/Notes/dependent-new.md",
            "dependent",
            dependencies=(missing,),
        ),
        _object(
            "s-c8",
            "Knowledge Base/_Governance/rules/source.yaml",
            "rules: []",
            kind="policy",
        ),
        # C8 outranks an otherwise exact duplicate.
        _object(
            "s-c8-overlap",
            "Knowledge Base/Notes/authority-exact.md",
            "same",
            kind="authorization_session",
        ),
        # C6 outranks a same-path C5 candidate.
        _object(
            "s-c6-overlap",
            "Knowledge Base/Notes/identity-destination.md",
            "different",
            identity="id-c6",
        ),
    )

    result = reconciliation.reconcile_inventories(source, destination)

    assert {row.source_object_ref: row.primary_class for row in result.rows} == {
        "s-c1": "C1",
        "s-c2": "C2",
        "s-c3": "C3",
        "s-c4": "C4",
        "s-c5": "C5",
        "s-c6": "C6",
        "s-c7": "C7",
        "s-c8": "C8",
        "s-c8-overlap": "C8",
        "s-c6-overlap": "C6",
    }
    c7 = next(row for row in result.rows if row.source_object_ref == "s-c7")
    assert [finding.code for finding in c7.dependency_findings] == ["DEPENDENCY_TARGET_MISSING"]
    assert result.source_snapshot_digest == source.snapshot_digest
    assert result.destination_snapshot_digest == destination.snapshot_digest


def test_order_and_digest_do_not_depend_on_input_or_dictionary_order() -> None:
    objects = (
        _object("z", "Knowledge Base/Notes/z.md", "z"),
        _object("a", "Knowledge Base/Notes/a.md", "a"),
        _object("m", "Knowledge Base/Notes/m.md", "m"),
    )
    destination = _inventory("destination")
    expected = reconciliation.reconcile_inventories(_inventory("source", *objects), destination)

    for permutation in itertools.permutations(objects):
        actual = reconciliation.reconcile_inventories(
            _inventory("source", *permutation), destination
        )
        assert actual.rows == expected.rows
        assert actual.digest == expected.digest


@pytest.mark.parametrize(
    ("identity_match", "path_match", "bytes_match", "logical_match", "expected"),
    [
        (True, True, False, False, "C6"),
        (False, True, False, False, "C5"),
        (False, False, False, True, "C4"),
        (True, False, True, False, "C3"),
        (False, True, True, False, "C1"),
        (False, False, False, False, "C2"),
    ],
)
def test_generated_direct_class_matrix_is_total(
    identity_match: bool,
    path_match: bool,
    bytes_match: bool,
    logical_match: bool,
    expected: str,
) -> None:
    source = _object(
        "source",
        "Knowledge Base/Notes/shared.md" if path_match else "Knowledge Base/Notes/source.md",
        "same" if bytes_match else "source",
        identity="identity" if identity_match else "source-identity",
        logical="logical" if logical_match else "source-logical",
    )
    destination = _object(
        "destination",
        "Knowledge Base/Notes/shared.md" if path_match else "Knowledge Base/Notes/dest.md",
        "same" if bytes_match else "destination",
        identity="identity" if identity_match else "destination-identity",
        logical="logical" if logical_match else "destination-logical",
    )

    row = reconciliation.reconcile_inventories(
        _inventory("source", source), _inventory("destination", destination)
    ).rows[0]

    assert row.primary_class == expected
    assert len(row.allowed_resolutions) >= 1


def test_c1_c2_c3_defaults_are_lossless_and_emit_c1_mapping_digest() -> None:
    destination = _inventory(
        "destination",
        _object("d1", "Knowledge Base/Notes/exact.md", "same", identity="id-1"),
        _object("d3", "Knowledge Base/Notes/relocated.md", "move", identity="id-3"),
    )
    source = _inventory(
        "source",
        _object("s1", "Knowledge Base/Notes/exact.md", "same", identity="id-1"),
        _object("s2", "Knowledge Base/Notes/new.md", "new", identity="id-2"),
        _object("s3", "Knowledge Base/Notes/old.md", "move", identity="id-3"),
    )
    result = reconciliation.reconcile_inventories(source, destination)

    tentative = reconciliation.validate_tentative_map(result, resolutions=())

    assert tentative.unresolved_count == 0
    assert [entry.action for entry in tentative.entries] == [
        "deduplicate_exact",
        "add",
        "reuse_destination",
    ]
    assert tentative.c1_mapping_digest != _digest("")
    c1 = tentative.entries[0]
    assert c1.source_sha256 == c1.destination_sha256
    assert c1.source_path == c1.destination_path


@pytest.mark.parametrize("primary_class", ["C4", "C5", "C6", "C7", "C8"])
def test_conflict_classes_remain_blocked_without_owner_resolution(
    primary_class: str,
) -> None:
    source, destination = _class_fixture(primary_class)
    result = reconciliation.reconcile_inventories(source, destination)

    with pytest.raises(reconciliation.ReconciliationUnresolved):
        reconciliation.validate_tentative_map(result, resolutions=())


def test_c7_dependency_blocks_otherwise_unique_object_and_cannot_disappear() -> None:
    dependency = reconciliation.ObjectDependency(
        dependency_ref="typed-edge",
        dependency_kind="typed_relation",
        target_identity="missing-id",
        relation_type="supports",
    )
    source_object = _object(
        "source",
        "Knowledge Base/Notes/new.md",
        "new",
        dependencies=(dependency,),
    )
    result = reconciliation.reconcile_inventories(
        _inventory("source", source_object), _inventory("destination")
    )

    assert len(result.rows) == 1
    assert result.rows[0].primary_class == "C7"
    assert result.rows[0].dependency_findings[0].dependency_ref == "typed-edge"


def test_append_only_collision_only_allows_byte_preserving_relocation() -> None:
    source = _object(
        "source",
        "Knowledge Base/Sources/capture.md",
        "source bytes",
        kind="source",
        append_only=True,
    )
    destination = _object(
        "destination",
        "Knowledge Base/Sources/capture.md",
        "destination bytes",
        kind="source",
        append_only=True,
    )
    result = reconciliation.reconcile_inventories(
        _inventory("source", source), _inventory("destination", destination)
    )
    assert result.rows[0].primary_class == "C5"
    assert result.rows[0].allowed_resolutions == ("relocate_preserving_bytes",)

    tentative = reconciliation.validate_tentative_map(
        result,
        resolutions=(
            reconciliation.OwnerResolution(
                source_object_ref="source",
                action="relocate_preserving_bytes",
                destination_path="Knowledge Base/Sources/capture-imported.md",
            ),
        ),
    )
    assert tentative.entries[0].source_sha256 == source.sha256
    assert tentative.entries[0].destination_sha256 == source.sha256


def test_append_only_body_rewrite_and_overwrite_are_rejected() -> None:
    source, destination = _class_fixture("C5", append_only=True)
    result = reconciliation.reconcile_inventories(source, destination)

    for action in ("overwrite_destination", "rewrite_body"):
        with pytest.raises(reconciliation.InvalidResolution):
            reconciliation.validate_tentative_map(
                result,
                resolutions=(
                    reconciliation.OwnerResolution(
                        source_object_ref=result.rows[0].source_object_ref,
                        action=action,
                        destination_path=result.rows[0].source_path,
                    ),
                ),
            )


def test_append_only_identity_collision_can_only_remain_in_provenance() -> None:
    source = _object(
        "source",
        "Knowledge Base/Evidence/source.md",
        "source evidence",
        identity="same-id",
        kind="evidence",
        append_only=True,
    )
    destination = _object(
        "destination",
        "Knowledge Base/Evidence/destination.md",
        "destination evidence",
        identity="same-id",
        kind="evidence",
        append_only=True,
    )
    result = reconciliation.reconcile_inventories(
        _inventory("source", source), _inventory("destination", destination)
    )

    assert result.rows[0].primary_class == "C6"
    assert result.rows[0].allowed_resolutions == ("retain_provenance_only",)


def test_case_colliding_destination_paths_are_rejected() -> None:
    source = _inventory(
        "source",
        _object("a", "Knowledge Base/Notes/A.md", "a"),
        _object("b", "Knowledge Base/Notes/a.md", "b"),
    )

    with pytest.raises(reconciliation.InventoryInvalid):
        reconciliation.reconcile_inventories(source, _inventory("destination"))


def test_markdown_builder_removes_only_exomem_id_for_logical_digest() -> None:
    a = reconciliation.inventory_object_from_bytes(
        object_ref="a",
        path="Knowledge Base/Notes/a.md",
        content=b"---\ntype: insight\nexomem_id: 00000000-0000-0000-0000-000000000001\n---\nBody.\n",
    )
    b = reconciliation.inventory_object_from_bytes(
        object_ref="b",
        path="Knowledge Base/Notes/b.md",
        content=b"---\ntype: insight\nexomem_id: 00000000-0000-0000-0000-000000000002\n---\nBody.\n",
    )
    changed = reconciliation.inventory_object_from_bytes(
        object_ref="changed",
        path="Knowledge Base/Notes/c.md",
        content=b"---\ntype: decision\nexomem_id: 00000000-0000-0000-0000-000000000003\n---\nBody.\n",
    )

    assert a.logical_content_sha256 == b.logical_content_sha256
    assert a.logical_content_sha256 != changed.logical_content_sha256
    assert a.durable_identity != b.durable_identity


def test_resolution_rewrites_only_declared_dependencies_and_rejects_dangling_map() -> None:
    dependency = reconciliation.ObjectDependency(
        dependency_ref="link",
        dependency_kind="wikilink",
        target_identity="target-id",
    )
    target = _object(
        "target",
        "Knowledge Base/Notes/target.md",
        "target",
        identity="target-id",
    )
    source = _inventory(
        "source",
        target,
        _object(
            "dependent",
            "Knowledge Base/Notes/dependent.md",
            "dependent",
            dependencies=(dependency,),
        ),
    )
    result = reconciliation.reconcile_inventories(source, _inventory("destination"))
    tentative = reconciliation.validate_tentative_map(result, resolutions=())

    rewrite = next(item for item in tentative.dependency_map if item.dependency_ref == "link")
    assert rewrite.target_identity == "target-id"
    assert rewrite.target_path == "Knowledge Base/Notes/target.md"

    broken = replace(
        result,
        rows=tuple(row for row in result.rows if row.source_object_ref != "target"),
    )
    with pytest.raises(reconciliation.InvalidResolution):
        reconciliation.validate_tentative_map(broken, resolutions=())


def test_authority_can_only_be_retained_as_non_executable_provenance() -> None:
    source, destination = _class_fixture("C8")
    result = reconciliation.reconcile_inventories(source, destination)
    row = result.rows[0]
    assert row.allowed_resolutions == ("retain_provenance_only",)

    tentative = reconciliation.validate_tentative_map(
        result,
        resolutions=(
            reconciliation.OwnerResolution(
                source_object_ref=row.source_object_ref,
                action="retain_provenance_only",
            ),
        ),
    )
    assert tentative.entries[0].publish is False
    assert tentative.entries[0].destination_path is None


def test_exact_duplicate_requires_both_bundle_and_object_bytes() -> None:
    shared_bundle = _digest("bundle")
    source = replace(
        _object("source", "Knowledge Base/Notes/page.md", "source"),
        bundle_sha256=shared_bundle,
    )
    destination = replace(
        _object("destination", "Knowledge Base/Notes/page.md", "destination"),
        bundle_sha256=shared_bundle,
    )

    row = reconciliation.reconcile_inventories(
        _inventory("source", source), _inventory("destination", destination)
    ).rows[0]

    assert row.primary_class == "C5"


def test_constructed_inventory_and_reconciliation_digest_tampering_fail_closed() -> None:
    source = _inventory("source", _object("source", "Knowledge Base/Notes/page.md", "page"))
    destination = _inventory("destination")
    forged_inventory = replace(source, digest=_digest("forged"))
    with pytest.raises(reconciliation.InventoryInvalid):
        reconciliation.reconcile_inventories(forged_inventory, destination)

    result = reconciliation.reconcile_inventories(source, destination)
    with pytest.raises(reconciliation.InvalidResolution):
        reconciliation.validate_tentative_map(
            replace(result, digest=_digest("forged")), resolutions=()
        )


def test_markdown_builder_uses_canonical_semantic_anchor_relation_and_link_parsers() -> None:
    item = reconciliation.inventory_object_from_bytes(
        object_ref="page",
        path="Knowledge Base/Notes/page.md",
        content=(
            b"---\n"
            b"type: insight\n"
            b"exomem_id: 00000000-0000-0000-0000-000000000001\n"
            b"---\n"
            b"# Page\n\n"
            b"- [claim] Anchored claim. ^claim-one\n\n"
            b"## Relations\n\n"
            b"- supports [[Knowledge Base/Notes/target]]\n\n"
            b"See [[Knowledge Base/Notes/other#anchor-two]].\n"
        ),
    )

    assert item.anchors == ("claim-one",)
    assert {
        (value.dependency_kind, value.target_path, value.target_anchor)
        for value in item.dependencies
    } == {
        ("typed_relation", "Knowledge Base/Notes/target.md", None),
        ("wikilink", "Knowledge Base/Notes/other.md", "anchor-two"),
    }


@pytest.mark.parametrize(
    ("path", "kind"),
    [
        ("Knowledge Base/_Governance/rules/a.yaml", "content"),
        ("Knowledge Base/_Consolidation/runs/a.json", "content"),
        ("Knowledge Base/.review-state.json", "content"),
        ("Knowledge Base/Notes/a.md", "derived_index"),
        ("Knowledge Base/Notes/a.md", "receipt"),
    ],
)
def test_authority_and_derived_state_always_receive_c8(path: str, kind: str) -> None:
    source = _object("source", path, "state", kind=kind)

    row = reconciliation.reconcile_inventories(
        _inventory("source", source), _inventory("destination")
    ).rows[0]

    assert row.primary_class == "C8"
    assert row.allowed_resolutions == ("retain_provenance_only",)


def test_malformed_semantic_structure_is_not_silently_inventoried() -> None:
    with pytest.raises(reconciliation.InventoryInvalid):
        reconciliation.inventory_object_from_bytes(
            object_ref="page",
            path="Knowledge Base/Notes/page.md",
            content=(
                b"---\ntype: insight\n---\n"
                b"## Relations\n\n- this is not a registered relation row\n"
            ),
        )


def test_c4_owner_can_reuse_destination_identity_without_publishing_duplicate() -> None:
    source, destination = _class_fixture("C4")
    result = reconciliation.reconcile_inventories(source, destination)
    row = result.rows[0]

    tentative = reconciliation.validate_tentative_map(
        result,
        resolutions=(
            reconciliation.OwnerResolution(
                source_object_ref=row.source_object_ref,
                action="use_destination_identity",
            ),
        ),
    )

    assert tentative.entries[0].publish is False
    assert tentative.entries[0].destination_object_ref == "destination"
    assert tentative.entries[0].destination_identity == "destination-id"


def test_c6_reidentity_requires_new_path_and_identity_and_preserves_source_hash() -> None:
    source, destination = _class_fixture("C6")
    result = reconciliation.reconcile_inventories(source, destination)
    row = result.rows[0]
    incomplete = reconciliation.OwnerResolution(
        source_object_ref=row.source_object_ref,
        action="reidentify_and_relocate",
        destination_path="Knowledge Base/Notes/imported.md",
    )
    with pytest.raises(reconciliation.InvalidResolution):
        reconciliation.validate_tentative_map(result, resolutions=(incomplete,))

    tentative = reconciliation.validate_tentative_map(
        result,
        resolutions=(replace(incomplete, destination_identity="new-id"),),
    )
    assert tentative.entries[0].source_sha256 == tentative.entries[0].destination_sha256
    assert tentative.entries[0].destination_identity == "new-id"


def test_c7_explicit_dependency_mapping_is_closed_and_deterministic() -> None:
    dependency = reconciliation.ObjectDependency(
        dependency_ref="missing-link",
        dependency_kind="wikilink",
        target_path="Knowledge Base/Notes/old-name.md",
    )
    source = _inventory(
        "source",
        _object(
            "dependent",
            "Knowledge Base/Notes/dependent.md",
            "dependent",
            dependencies=(dependency,),
        ),
    )
    destination = _inventory(
        "destination",
        _object("chosen", "Knowledge Base/Notes/chosen.md", "chosen", identity="chosen-id"),
    )
    result = reconciliation.reconcile_inventories(source, destination)

    tentative = reconciliation.validate_tentative_map(
        result,
        resolutions=(
            reconciliation.OwnerResolution(
                source_object_ref="dependent",
                action="map_dependencies",
                dependency_targets=(
                    reconciliation.DependencyResolution(
                        dependency_ref="missing-link",
                        target_destination_object_ref="chosen",
                    ),
                ),
            ),
        ),
    )

    assert tentative.dependency_map == (
        reconciliation.DependencyMapping(
            source_object_ref="dependent",
            dependency_ref="missing-link",
            dependency_kind="wikilink",
            target_object_ref="chosen",
            target_path="Knowledge Base/Notes/chosen.md",
            target_identity="chosen-id",
            target_anchor=None,
        ),
    )


def test_resolution_cannot_publish_two_objects_to_one_folded_path_or_identity() -> None:
    destination = _inventory(
        "destination",
        _object("d-a", "Knowledge Base/Notes/a.md", "destination-a"),
        _object("d-b", "Knowledge Base/Notes/b.md", "destination-b"),
    )
    source = _inventory(
        "source",
        _object("s-a", "Knowledge Base/Notes/a.md", "source-a"),
        _object("s-b", "Knowledge Base/Notes/b.md", "source-b"),
    )
    result = reconciliation.reconcile_inventories(source, destination)
    resolutions = tuple(
        reconciliation.OwnerResolution(
            source_object_ref=row.source_object_ref,
            action="relocate_preserving_bytes",
            destination_path="Knowledge Base/Notes/Collision.md"
            if row.source_object_ref == "s-a"
            else "Knowledge Base/Notes/collision.md",
        )
        for row in result.rows
    )

    with pytest.raises(reconciliation.InvalidResolution):
        reconciliation.validate_tentative_map(result, resolutions=resolutions)


def test_inventory_derives_append_only_from_path_and_rejects_spoofed_metadata() -> None:
    spoofed = _object(
        "source",
        "Knowledge Base/Sources/capture.md",
        "immutable source",
        kind="source",
        append_only=False,
    )

    with pytest.raises(reconciliation.InventoryInvalid):
        _inventory("source", spoofed)


def test_markdown_builder_preserves_same_page_anchor_and_frontmatter_dependencies() -> None:
    item = reconciliation.inventory_object_from_bytes(
        object_ref="page",
        path="Knowledge Base/Notes/page.md",
        content=(
            b"---\n"
            b"type: insight\n"
            b"exomem_id: 00000000-0000-4000-8000-000000000001\n"
            b"sources:\n"
            b"  - Knowledge Base/Sources/source.md\n"
            b"supersedes: '[[Knowledge Base/Notes/old]]'\n"
            b"evidence_file: Knowledge Base/Evidence/clip.png\n"
            b"---\n"
            b"See [[#missing-anchor]].\n"
        ),
    )

    assert {
        (value.dependency_kind, value.target_path, value.target_identity, value.target_anchor)
        for value in item.dependencies
    } == {
        ("citation", "Knowledge Base/Sources/source.md", None, None),
        ("supersession", "Knowledge Base/Notes/old.md", None, None),
        ("media_pair", "Knowledge Base/Evidence/clip.png", None, None),
        ("wikilink", "Knowledge Base/Notes/page.md", None, "missing-anchor"),
    }

    result = reconciliation.reconcile_inventories(
        _inventory("source", item), _inventory("destination")
    )
    assert result.rows[0].primary_class == "C7"
    assert any(
        finding.code == "DEPENDENCY_ANCHOR_MISSING"
        for finding in result.rows[0].dependency_findings
    )


def test_c1_dependency_maps_to_surviving_destination_object() -> None:
    dependency = reconciliation.ObjectDependency(
        dependency_ref="exact-target",
        dependency_kind="wikilink",
        target_path="Knowledge Base/Notes/target.md",
    )
    source = _inventory(
        "source",
        _object("source-target", "Knowledge Base/Notes/target.md", "same"),
        _object(
            "dependent",
            "Knowledge Base/Notes/dependent.md",
            "dependent",
            dependencies=(dependency,),
        ),
    )
    destination = _inventory(
        "destination",
        _object("destination-target", "Knowledge Base/Notes/target.md", "same"),
    )

    tentative = reconciliation.validate_tentative_map(
        reconciliation.reconcile_inventories(source, destination), resolutions=()
    )

    mapped = next(
        value for value in tentative.dependency_map if value.dependency_ref == "exact-target"
    )
    assert mapped.target_object_ref == "destination-target"


def test_default_content_action_accepts_dependency_only_owner_resolution() -> None:
    dependency = reconciliation.ObjectDependency(
        dependency_ref="missing",
        dependency_kind="wikilink",
        target_path="Knowledge Base/Notes/missing.md",
    )
    source_object = _object(
        "source",
        "Knowledge Base/Notes/exact.md",
        "same",
        dependencies=(dependency,),
    )
    destination_exact = _object("exact", "Knowledge Base/Notes/exact.md", "same")
    chosen = _object("chosen", "Knowledge Base/Notes/chosen.md", "chosen")
    result = reconciliation.reconcile_inventories(
        _inventory("source", source_object),
        _inventory("destination", destination_exact, chosen),
    )

    tentative = reconciliation.validate_tentative_map(
        result,
        resolutions=(
            reconciliation.OwnerResolution(
                source_object_ref="source",
                action="deduplicate_exact",
                dependency_targets=(
                    reconciliation.DependencyResolution(
                        dependency_ref="missing",
                        target_destination_object_ref="chosen",
                    ),
                ),
            ),
        ),
    )

    assert tentative.entries[0].action == "deduplicate_exact"
    assert tentative.dependency_map[0].target_object_ref == "chosen"


def test_multi_match_requires_exact_destination_selector() -> None:
    source = _object(
        "source",
        "Knowledge Base/Notes/source.md",
        "source",
        identity="source-id",
        logical="shared",
    )
    destinations = (
        _object(
            "destination-a",
            "Knowledge Base/Notes/a.md",
            "a",
            identity="destination-a",
            logical="shared",
        ),
        _object(
            "destination-b",
            "Knowledge Base/Notes/b.md",
            "b",
            identity="destination-b",
            logical="shared",
        ),
    )
    result = reconciliation.reconcile_inventories(
        _inventory("source", source), _inventory("destination", *destinations)
    )
    without_selector = reconciliation.OwnerResolution(
        source_object_ref="source", action="use_destination_identity"
    )
    with pytest.raises(reconciliation.InvalidResolution):
        reconciliation.validate_tentative_map(result, resolutions=(without_selector,))

    selected = reconciliation.validate_tentative_map(
        result,
        resolutions=(replace(without_selector, destination_object_ref="destination-b"),),
    )
    assert selected.entries[0].destination_object_ref == "destination-b"


def test_duplicate_destination_durable_identity_blocks_plan_materialization() -> None:
    destination = _inventory(
        "destination",
        _object("a", "Knowledge Base/Notes/a.md", "a", identity="duplicate"),
        _object("b", "Knowledge Base/Notes/b.md", "b", identity="duplicate"),
    )
    result = reconciliation.reconcile_inventories(
        _inventory("source", _object("source", "Knowledge Base/Notes/new.md", "new")),
        destination,
    )

    with pytest.raises(reconciliation.InvalidResolution):
        reconciliation.validate_tentative_map(result, resolutions=())


@pytest.mark.parametrize(
    ("source_kind", "destination_kind", "source_lifecycle", "destination_lifecycle"),
    [
        ("content", "media_sidecar", None, None),
        ("content", "content", "active", "archived"),
    ],
)
def test_replace_destination_rejects_object_kind_or_lifecycle_transition(
    source_kind: str,
    destination_kind: str,
    source_lifecycle: str | None,
    destination_lifecycle: str | None,
) -> None:
    source = _object(
        "source",
        "Knowledge Base/Notes/item.md",
        "content bytes",
        kind=source_kind,
        lifecycle=source_lifecycle,
    )
    destination = _object(
        "destination",
        source.path,
        "manifest bytes",
        kind=destination_kind,
        lifecycle=destination_lifecycle,
    )
    result = reconciliation.reconcile_inventories(
        _inventory("source", source), _inventory("destination", destination)
    )

    with pytest.raises(reconciliation.InvalidResolution):
        reconciliation.validate_tentative_map(
            result,
            resolutions=(
                reconciliation.OwnerResolution(
                    source_object_ref="source",
                    action="replace_destination_exact",
                    destination_object_ref="destination",
                ),
            ),
        )


def test_c1_mapping_digest_binds_snapshots_inventories_and_bundle_commitments() -> None:
    def mapping(*, source_snapshot: str, destination_snapshot: str, bundle: str) -> str:
        source_object = replace(
            _object("source", "Knowledge Base/Notes/exact.md", "same", identity="same-id"),
            bundle_sha256=_digest(bundle),
        )
        destination_object = replace(
            _object(
                "destination",
                "Knowledge Base/Notes/exact.md",
                "same",
                identity="same-id",
            ),
            bundle_sha256=_digest(bundle),
        )
        source = reconciliation.build_inventory(
            (source_object,), role="source", snapshot_digest=_digest(source_snapshot)
        )
        destination = reconciliation.build_inventory(
            (destination_object,),
            role="destination",
            snapshot_digest=_digest(destination_snapshot),
        )
        result = reconciliation.reconcile_inventories(source, destination)
        return reconciliation.validate_tentative_map(result, resolutions=()).c1_mapping_digest

    baseline = mapping(source_snapshot="s1", destination_snapshot="d1", bundle="bundle-a")
    assert baseline != mapping(source_snapshot="s2", destination_snapshot="d1", bundle="bundle-a")
    assert baseline != mapping(source_snapshot="s1", destination_snapshot="d2", bundle="bundle-a")
    assert baseline != mapping(source_snapshot="s1", destination_snapshot="d1", bundle="bundle-b")


def test_canonical_reverse_citation_and_evidence_aliases_remain_distinct_from_media() -> None:
    item = reconciliation.inventory_object_from_bytes(
        object_ref="source",
        path="Knowledge Base/Sources/source.md",
        content=(
            b"---\n"
            b"type: source\n"
            b"ingested_into: ['[[Knowledge Base/Notes/compiled]]']\n"
            b"evidence: Knowledge Base/Evidence/proof.md\n"
            b"evidences: [Knowledge Base/Evidence/second.md]\n"
            b"evidence_paths: [Knowledge Base/Evidence/third.md]\n"
            b"evidence_file: Knowledge Base/Evidence/raw.pdf\n"
            b"---\n"
            b"Captured source.\n"
        ),
    )

    assert {(value.dependency_kind, value.target_path) for value in item.dependencies} == {
        ("reverse_citation", "Knowledge Base/Notes/compiled.md"),
        ("citation", "Knowledge Base/Evidence/proof.md"),
        ("citation", "Knowledge Base/Evidence/second.md"),
        ("citation", "Knowledge Base/Evidence/third.md"),
        ("media_pair", "Knowledge Base/Evidence/raw.pdf"),
    }


def _valid_records_manifest() -> bytes:
    return (
        b"---\n"
        b"type: collection\n"
        b"exomem_id: 44444444-4444-4444-8444-444444444444\n"
        b"title: New records\n"
        b"semantic_profile: records\n"
        b"collection_version: 1\n"
        b"schema_version: 1\n"
        b"lifecycle: active\n"
        b"storage:\n"
        b"  strategy: markdown-items\n"
        b"  source: Events\n"
        b"  format_version: 1\n"
        b"item_schema:\n"
        b"  natural_key: [occurred_on]\n"
        b"  fields:\n"
        b"    occurred_on:\n"
        b"      type: date\n"
        b"      required: true\n"
        b"---\n"
    )


def _valid_markdown_log_manifest() -> bytes:
    return (
        b"---\n"
        b"type: collection\n"
        b"exomem_id: 88888888-8888-4888-8888-888888888888\n"
        b"title: Training sessions\n"
        b"semantic_profile: records\n"
        b"collection_version: 1\n"
        b"schema_version: 1\n"
        b"lifecycle: active\n"
        b"storage:\n"
        b"  strategy: markdown-log\n"
        b"  source: Training Log.md\n"
        b"  format_version: 1\n"
        b"  section:\n"
        b"    level: 2\n"
        b"    title: Sessions\n"
        b"  item_heading:\n"
        b"    level: 3\n"
        b"    fields:\n"
        b"      - name: occurred_on\n"
        b"        type: date\n"
        b"        format: '%Y-%m-%d'\n"
        b"    separator: ' - '\n"
        b"  insertion: newest-first\n"
        b"  child_rows:\n"
        b"    prefix: '- '\n"
        b"    delimiter: '|'\n"
        b"    fields: [movement, repetitions]\n"
        b"    container_field: movements\n"
        b"item_schema:\n"
        b"  natural_key: [occurred_on]\n"
        b"  fields:\n"
        b"    occurred_on:\n"
        b"      type: date\n"
        b"      required: true\n"
        b"    movements:\n"
        b"      type: array\n"
        b"      items:\n"
        b"        type: object\n"
        b"---\n"
    )


def test_records_inventory_uses_canonical_manifest_and_item_contracts() -> None:
    manifest = reconciliation.inventory_object_from_bytes(
        object_ref="manifest",
        path="Knowledge Base/Records/New/_collection.md",
        content=_valid_records_manifest(),
    )
    item = reconciliation.inventory_object_from_bytes(
        object_ref="item",
        path="Knowledge Base/Records/New/Events/item.md",
        content=(
            b"---\n"
            b"type: record\n"
            b"collection_id: 44444444-4444-4444-8444-444444444444\n"
            b"record_id: 55555555-5555-4555-8555-555555555555\n"
            b"schema_version: 1\n"
            b"occurred_on: 2026-08-28\n"
            b"---\n"
            b"Record body.\n"
        ),
    )

    inventory = _inventory("source", manifest, item)
    by_ref = {value.object_ref: value for value in inventory.objects}
    assert by_ref["manifest"].object_kind == "record_manifest"
    assert by_ref["item"].object_kind == "record_item"
    assert by_ref["item"].durable_identity == (
        "record:44444444-4444-4444-8444-444444444444:55555555-5555-4555-8555-555555555555"
    )
    assert by_ref["item"].lifecycle is None


def test_records_inventory_rejects_malformed_manifest_and_non_record_item() -> None:
    with pytest.raises(reconciliation.InventoryInvalid):
        reconciliation.inventory_object_from_bytes(
            object_ref="bad-manifest",
            path="Knowledge Base/Records/New/_collection.md",
            content=b"---\ntype: collection\n---\n",
        )

    manifest = reconciliation.inventory_object_from_bytes(
        object_ref="manifest",
        path="Knowledge Base/Records/New/_collection.md",
        content=_valid_records_manifest(),
    )
    impostor = reconciliation.inventory_object_from_bytes(
        object_ref="impostor",
        path="Knowledge Base/Records/New/Events/not-a-record.md",
        content=b"---\ntype: insight\n---\nNot a Record.\n",
    )
    with pytest.raises(reconciliation.InventoryInvalid):
        _inventory("source", manifest, impostor)


def test_records_inventory_preserves_numeric_values_and_item_lifecycle() -> None:
    manifest = reconciliation.inventory_object_from_bytes(
        object_ref="manifest",
        path="Knowledge Base/Records/Vehicle/_collection.md",
        content=(
            b"---\n"
            b"type: collection\n"
            b"exomem_id: 66666666-6666-4666-8666-666666666666\n"
            b"title: Vehicle maintenance\n"
            b"semantic_profile: records\n"
            b"collection_version: 1\n"
            b"schema_version: 1\n"
            b"lifecycle: active\n"
            b"storage:\n"
            b"  strategy: markdown-items\n"
            b"  source: Events\n"
            b"  format_version: 1\n"
            b"item_schema:\n"
            b"  natural_key: [occurred_on]\n"
            b"  fields:\n"
            b"    occurred_on:\n"
            b"      type: date\n"
            b"      required: true\n"
            b"    amount:\n"
            b"      type: number\n"
            b"      required: true\n"
            b"    lifecycle:\n"
            b"      type: enum\n"
            b"      enum: [active, archived]\n"
            b"---\n"
        ),
    )
    item = reconciliation.inventory_object_from_bytes(
        object_ref="item",
        path="Knowledge Base/Records/Vehicle/Events/service.md",
        content=(
            b"---\n"
            b"type: record\n"
            b"collection_id: 66666666-6666-4666-8666-666666666666\n"
            b"record_id: 77777777-7777-4777-8777-777777777777\n"
            b"schema_version: 1\n"
            b"occurred_on: 2026-08-28\n"
            b"amount: 92.5\n"
            b"lifecycle: archived\n"
            b"---\n"
            b"Service event.\n"
        ),
    )

    inventory = _inventory("source", manifest, item)
    record = next(value for value in inventory.objects if value.object_ref == "item")
    assert record.lifecycle == "archived"
    assert record.record_values_jcs is not None
    assert '"amount":92.5' in record.record_values_jcs


def test_records_inventory_binds_all_markdown_log_storage_components() -> None:
    fixture = Path(__file__).parent / "fixtures" / "records" / "x3"
    root = "Knowledge Base/Records/Health/X3"
    objects = (
        reconciliation.inventory_object_from_bytes(
            object_ref="manifest",
            path=f"{root}/_collection.md",
            content=(fixture / "_collection.md").read_bytes(),
        ),
        reconciliation.inventory_object_from_bytes(
            object_ref="live-log",
            path=f"{root}/Training Log.md",
            content=(fixture / "Training Log.md").read_bytes(),
        ),
        reconciliation.inventory_object_from_bytes(
            object_ref="archive",
            path=f"{root}/Historical Reps (undated).md",
            content=(fixture / "Historical Reps (undated).md").read_bytes(),
        ),
    )

    inventory = _inventory("source", *objects)
    by_ref = {value.object_ref: value for value in inventory.objects}
    assert by_ref["manifest"].record_storage_components == (
        f"{root}/Historical Reps (undated).md",
        f"{root}/Training Log.md",
    )
    assert by_ref["live-log"].object_kind == "record_item"
    assert by_ref["archive"].object_kind == "record_item"


def test_divergent_aggregate_record_container_cannot_replace_destination() -> None:
    root = "Knowledge Base/Records/Training"
    manifest_bytes = _valid_markdown_log_manifest()
    log_bytes = b"---\ntype: tracker\n---\n# Training\n\n## Sessions\n"

    def inventory(role: str, *, source_log: bytes) -> reconciliation.ReconciliationInventory:
        return _inventory(
            role,
            reconciliation.inventory_object_from_bytes(
                object_ref=f"{role}-manifest",
                path=f"{root}/_collection.md",
                content=manifest_bytes,
            ),
            reconciliation.inventory_object_from_bytes(
                object_ref=f"{role}-log",
                path=f"{root}/Training Log.md",
                content=source_log,
            ),
        )

    result = reconciliation.reconcile_inventories(
        inventory("source", source_log=log_bytes + b"\n### 2026-08-28 - Source only\n"),
        inventory("destination", source_log=log_bytes),
    )
    row = next(value for value in result.rows if value.source_object_ref == "source-log")
    assert row.primary_class == "C5"
    assert row.allowed_resolutions == ("relocate_preserving_bytes",)

    with pytest.raises(reconciliation.InvalidResolution):
        reconciliation.validate_tentative_map(
            result,
            resolutions=(
                reconciliation.OwnerResolution(
                    source_object_ref="source-log",
                    action="replace_destination_exact",
                    destination_object_ref="destination-log",
                ),
            ),
        )

    tentative = reconciliation.validate_tentative_map(
        result,
        resolutions=(
            reconciliation.OwnerResolution(
                source_object_ref="source-log",
                action="relocate_preserving_bytes",
                destination_object_ref="destination-log",
                destination_path=f"{root}/Imported Training Log.md",
            ),
        ),
    )
    log_entry = next(
        value for value in tentative.entries if value.source_object_ref == "source-log"
    )
    assert log_entry.action == "relocate_preserving_bytes"
    assert log_entry.destination_path == f"{root}/Imported Training Log.md"
