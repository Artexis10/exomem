"""Product-shape acceptance for the synthetic context-activation corpus.

These tests stop before scoring.  A corpus is eligible for the real-compiler
gate only when its logical keys name canonical governed structures and those
structures are visible to a freshly published activation index.
"""

from __future__ import annotations

import dataclasses
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from epistemic.corpora.context_activation import (
    FixtureError,
    _corpus_hash,
    _governed_resource,
    build_corpus,
    fixture_by_id,
    freeze_reference_binding,
)
from membench.utility.context_activation import (
    ActivationPacket,
    Anchor,
    CurrentStateEntry,
    ManifestVoidError,
    Unit,
    run_audit,
    score_case,
    validate_manifest,
)

from exomem import structured_collections, working_set_index
from exomem import vault as vault_module

pytestmark = pytest.mark.timeout(180)


@pytest.fixture(scope="module")
def product_corpus(tmp_path_factory: pytest.TempPathFactory):
    root = tmp_path_factory.mktemp("context-activation-product-corpus")
    return root, build_corpus(root, distractor_count=0)


def _frontmatter(root: Path, relative: str) -> dict[str, object]:
    text = (root / relative).read_text(encoding="utf-8")
    frontmatter, _body, _warnings = vault_module.parse_frontmatter(text)
    return frontmatter


def test_corpus_keys_resolve_to_canonical_product_structures(product_corpus) -> None:
    root, manifest = product_corpus

    assert all(path.startswith("Knowledge Base/") for path in manifest.key_to_path.values())

    for key in (
        "c4_entity_profile",
        "t4_shared_first_name_entity_a",
        "t4_shared_first_name_entity_b",
    ):
        frontmatter = _frontmatter(root, manifest.key_to_path[key])
        assert (frontmatter["type"], frontmatter["entity_type"]) == ("entity", "person")
        assert frontmatter["exomem_id"]

    for key in ("c7_hub_feature", "c7_hub_market", "c7_hub_search_ux"):
        frontmatter = _frontmatter(root, manifest.key_to_path[key])
        assert "hub" in frontmatter["tags"]

    for key in (
        "c2_grill_equipment_page",
        "t2_camera_gear_note",
        "c5_resource_profile",
        "t5_available_resource",
    ):
        relative = manifest.key_to_path[key]
        frontmatter = _frontmatter(root, relative)
        text = (root / relative).read_text(encoding="utf-8")
        assert str(frontmatter["created"]).endswith("Z")
        assert "## Observations" in text
        assert "- [resource]" in text

    for key in ("c1_subscriptions_collection", "c5_records_latest_unavailable"):
        relative = manifest.key_to_path[key]
        assert relative.startswith("Knowledge Base/Records/")
        collection = structured_collections.load_manifest(root, root / relative)
        assert collection.semantic_profile == "records"

    for key in ("c3_planning_item", "t3_other_project_planning_item"):
        relative = manifest.key_to_path[key]
        assert relative.startswith("Knowledge Base/Planning/")
        assert "/Items/" in relative
        assert "exomem-plan-audit" in (root / relative).read_text(encoding="utf-8")


def test_records_and_planning_are_populated_through_their_canonical_writers(
    product_corpus,
) -> None:
    root, manifest = product_corpus

    records_path = root / manifest.key_to_path["c5_records_latest_unavailable"]
    records_manifest = structured_collections.load_manifest(root, records_path)
    record_items = sorted((root / records_manifest.storage.source).glob("*.md"))
    assert len(record_items) == 2
    assert all("exomem-record-audit" in path.read_text(encoding="utf-8") for path in record_items)

    planning_manifests = sorted((root / "Knowledge Base" / "Planning").glob("*/_collection.md"))
    assert len(planning_manifests) == 2
    assert all(
        structured_collections.load_manifest(root, path).semantic_profile == "planning"
        for path in planning_manifests
    )


def test_fresh_activation_index_publishes_every_canonical_anchor_kind(product_corpus) -> None:
    root, manifest = product_corpus

    index = working_set_index.WorkingSetIndex(root)
    report = index.rebuild()
    anchors = index.anchors()
    by_path = {anchor.path: anchor for anchor in anchors}

    assert report["anchors"] == len(anchors)
    assert {anchor.kind for anchor in anchors} >= {"entity", "hub", "resource", "plan", "collection"}
    assert by_path[manifest.key_to_path["c4_entity_profile"]].kind == "entity"
    assert by_path[manifest.key_to_path["c7_hub_feature"]].kind == "hub"
    assert by_path[manifest.key_to_path["c1_subscriptions_collection"]].kind == "collection"
    assert any(
        anchor.kind == "plan" and anchor.title == "Extending the reporting module"
        for anchor in anchors
    )
    assert by_path[manifest.key_to_path["c5_resource_profile"]].kind == "resource"


def test_exact_corpus_hash_binds_canonical_entity_identity(tmp_path: Path) -> None:
    entity_path = tmp_path / "Knowledge Base" / "People" / "rowan-ashfield.md"
    entity_path.parent.mkdir(parents=True)
    original = """---
type: entity
entity_type: person
exomem_id: 11111111-1111-4111-8111-111111111111
title: Rowan Ashfield
---

# Rowan Ashfield
"""
    entity_path.write_text(original, encoding="utf-8")
    before = _corpus_hash(tmp_path)
    edited = re.sub(
        r"(?m)^exomem_id: [0-9a-f-]+$",
        "exomem_id: ffffffff-ffff-4fff-8fff-ffffffffffff",
        original,
        count=1,
    )
    assert edited != original
    entity_path.write_text(edited, encoding="utf-8")

    assert _corpus_hash(tmp_path) != before


def test_resource_fixture_refuses_to_fall_back_when_the_writer_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import commands

    def refuse(*_args, **_kwargs):
        raise RuntimeError("writer refused fixture")

    monkeypatch.setattr(commands, "op_manage_memory_file", refuse)

    with pytest.raises(RuntimeError, match="writer refused fixture"):
        _governed_resource(
            tmp_path,
            path="Knowledge Base/Systems/resource.md",
            title="Resource",
            observation="A governed resource.",
            tags=("resource",),
        )
    assert not (tmp_path / "Knowledge Base/Systems/resource.md").exists()


def test_public_builder_isolates_hostile_ambient_runtime_state(tmp_path: Path) -> None:
    repo_root = Path(__file__).parents[1]
    hostile = tmp_path / "hostile-ambient"
    corpus = tmp_path / "corpus"
    script = r'''
import json
import os
from pathlib import Path

from epistemic.corpora.context_activation import build_corpus
from exomem import writer_lease


def snapshot(root):
    return {
        path.relative_to(root).as_posix(): path.read_bytes().hex()
        for path in root.rglob("*")
        if path.is_file()
    }


hostile = Path(os.environ["CORPUS_TEST_HOSTILE_ROOT"])
manager = writer_lease.get_manager()
before_files = snapshot(hostile)
before_env = dict(os.environ)
manifest = build_corpus(Path(os.environ["CORPUS_TEST_OUTPUT"]), distractor_count=0)
assert snapshot(hostile) == before_files
assert dict(os.environ) == before_env
assert writer_lease.get_manager() is manager
print(json.dumps({"corpus_id": manifest.corpus_id, "paths": len(manifest.key_to_path)}))
'''
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("PYTEST_") and key != "PYTHONPATH"
    }
    env.update(
        {
            "PYTHONPATH": f"{repo_root / 'src'}:{repo_root / 'benchmarks'}",
            "CORPUS_TEST_HOSTILE_ROOT": str(hostile),
            "CORPUS_TEST_OUTPUT": str(corpus),
            "EXOMEM_STATE_ROOT": str(hostile / "state"),
            "EXOMEM_CONFIG_PATH": str(hostile / "config.json"),
            "EXOMEM_LOG_DIR": str(hostile / "logs"),
            "EXOMEM_CALL_LEDGER_DIR": str(hostile / "call-ledger"),
            "EXOMEM_WRITER_LEASE_STATE_DIR": str(hostile / "writer-lease"),
            "EXOMEM_LEASE_COORDINATOR_DB": str(hostile / "lease-coordinator.sqlite"),
            "XDG_STATE_HOME": str(hostile / "xdg-state"),
        }
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=repo_root,
        env=env,
        text=True,
        capture_output=True,
        timeout=180,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr[-4000:]
    assert '"paths": 24' in completed.stdout


def _projection_packet(binding, *keys: str) -> ActivationPacket:
    projections = {projection.anchor_key: projection for projection in binding.projections}
    selected = [projections[key] for key in keys]
    return ActivationPacket(
        anchors=tuple(
            Anchor(ref=projection.anchor_ref, title=projection.anchor_key, kind="resource", status="resolved")
            for projection in selected
        ),
        units=tuple(
            Unit(
                ref=projection.projection_ref,
                role="current_state",
                text=projection.statement,
                updated=projection.as_of,
                provenance={
                    "anchor": projection.anchor_ref,
                    "source": projection.source_kind,
                    "as_of": projection.as_of,
                },
            )
            for projection in selected
        ),
        current_state=tuple(
            CurrentStateEntry(
                anchor=projection.anchor_ref,
                source=projection.source_kind,
                as_of=projection.as_of,
                statement=projection.statement,
            )
            for projection in selected
        ),
    )


def test_frozen_binding_reads_authored_records_and_profile_state(product_corpus) -> None:
    root, manifest = product_corpus
    binding = freeze_reference_binding(root, manifest, manifest.key_to_path)
    projections = {projection.anchor_key: projection for projection in binding.projections}

    assert projections["c1_subscriptions_collection"].statement == "state: limit-reached"
    assert projections["c5_resource_profile"].source_key == "c5_records_latest_unavailable"
    assert projections["c5_resource_profile"].statement == "status: unavailable"
    assert projections["c5_records_latest_unavailable"].statement == "status: unavailable"
    assert projections["t5_available_resource"].statement == "status: active"
    assert projections["c2_grill_equipment_page"].statement == "status: active"
    assert projections["t2_camera_gear_note"].statement == "status: active"
    assert all(projection.source_snapshot_digest for projection in binding.projections)


def test_binding_reads_latest_canonical_record_independently_of_state_resolver(
    product_corpus, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import working_set_state

    root, manifest = product_corpus
    monkeypatch.setattr(
        working_set_state,
        "_from_records",
        lambda *_args, **_kwargs: {
            "source": "records",
            "as_of": "2026-08-02",
            "statement": "status: available",
        },
    )

    binding = freeze_reference_binding(root, manifest, manifest.key_to_path)
    projections = {projection.anchor_key: projection for projection in binding.projections}

    assert projections["c5_resource_profile"].as_of == "2026-09-10"
    assert projections["c5_resource_profile"].statement == "status: unavailable"


def test_c5_counts_two_gold_sources_and_two_bound_projections_independently(product_corpus) -> None:
    root, manifest = product_corpus
    binding = freeze_reference_binding(root, manifest, manifest.key_to_path)
    packet = _projection_packet(binding, "c5_resource_profile", "c5_records_latest_unavailable")

    score = score_case(packet, fixture_by_id("C5"), reference_binding=binding)

    assert score.gold_hit == score.gold_total == 2
    assert score.precision == 1.0


def test_t5_own_bound_profile_projection_is_not_false_activation(product_corpus) -> None:
    root, manifest = product_corpus
    binding = freeze_reference_binding(root, manifest, manifest.key_to_path)
    score = score_case(
        _projection_packet(binding, "t5_available_resource"),
        fixture_by_id("T5"),
        reference_binding=binding,
    )

    assert score.gold_hit == score.gold_total == 1
    assert score.precision == 1.0
    assert score.twin_false_activation is False


def test_bound_projection_alone_recalls_its_canonical_gold_identity(product_corpus) -> None:
    root, manifest = product_corpus
    binding = freeze_reference_binding(root, manifest, manifest.key_to_path)
    packet = dataclasses.replace(_projection_packet(binding, "t5_available_resource"), anchors=())

    score = score_case(packet, fixture_by_id("T5"), reference_binding=binding)

    assert score.gold_hit == score.gold_total == 1
    assert score.precision == 1.0


def test_unbound_person_current_projection_cannot_manufacture_relevance(product_corpus) -> None:
    root, manifest = product_corpus
    binding = freeze_reference_binding(root, manifest, manifest.key_to_path)
    base = manifest.key_to_path["c4_entity_profile"]
    packet = ActivationPacket(
        anchors=(Anchor(ref=base, title="person", kind="entity", status="resolved"),),
        units=(
            Unit(
                ref=f"{base}#current",
                role="current_state",
                text="status: active",
                provenance={"anchor": base, "source": "profile"},
            ),
        ),
        current_state=(CurrentStateEntry(anchor=base, source="profile", statement="status: active"),),
    )

    score = score_case(packet, fixture_by_id("C4"), reference_binding=binding)

    assert score.gold_hit == 1
    assert score.gold_total == 2
    assert score.precision == 0.5


@pytest.mark.parametrize(
    ("mutation",),
    [
        ("wrong_role",),
        ("missing_state",),
        ("mismatched_state",),
        ("lying_provenance",),
        ("missing_updated",),
        ("mismatched_updated",),
    ],
)
def test_projection_credit_requires_bound_packet_metadata(product_corpus, mutation: str) -> None:
    root, manifest = product_corpus
    binding = freeze_reference_binding(root, manifest, manifest.key_to_path)
    packet = _projection_packet(binding, "t5_available_resource")
    unit = packet.units[0]
    state = packet.current_state
    if mutation == "wrong_role":
        unit = dataclasses.replace(unit, role="profile")
    elif mutation == "missing_state":
        state = ()
    elif mutation == "mismatched_state":
        state = (dataclasses.replace(state[0], statement="status: unavailable"),)
    elif mutation == "missing_updated":
        unit = dataclasses.replace(unit, updated=None)
    elif mutation == "mismatched_updated":
        unit = dataclasses.replace(unit, updated="2026-01-01")
    else:
        unit = dataclasses.replace(unit, provenance={"anchor": "someone-else", "source": "profile"})
    packet = dataclasses.replace(packet, anchors=(), units=(unit,), current_state=state)

    score = score_case(packet, fixture_by_id("T5"), reference_binding=binding)

    assert score.gold_hit == 0
    assert score.precision == 0.0


def test_known_poison_projection_cannot_hide_with_wrong_role_or_supersession(product_corpus) -> None:
    root, manifest = product_corpus
    binding = freeze_reference_binding(root, manifest, manifest.key_to_path)
    packet = _projection_packet(binding, "c5_records_latest_unavailable")
    poisoned = dataclasses.replace(
        packet.units[0],
        role="other",
        lifecycle="superseded",
        provenance={"superseded_by": "invented-successor"},
    )
    packet = dataclasses.replace(packet, anchors=(), units=(poisoned,), current_state=())

    score = score_case(packet, fixture_by_id("T5"), reference_binding=binding)

    assert score.poison_hit == 1
    assert score.twin_false_activation is True


def test_current_state_only_canonical_poison_remains_a_poison_hit(product_corpus) -> None:
    root, manifest = product_corpus
    binding = freeze_reference_binding(root, manifest, manifest.key_to_path)
    poison = manifest.key_to_path["t5_available_resource"]
    packet = ActivationPacket(
        current_state=(
            CurrentStateEntry(
                anchor=poison,
                source="profile",
                statement="status: active",
            ),
        ),
    )

    score = score_case(packet, fixture_by_id("C5"), reference_binding=binding)

    assert score.poison_hit == 1
    resource_tally = next(row for row in score.by_anchor_kind if row.kind == "resource")
    assert resource_tally.poison_hit == 1


def test_true_unit_fragment_stays_a_distinct_precision_entry(product_corpus) -> None:
    root, manifest = product_corpus
    binding = freeze_reference_binding(root, manifest, manifest.key_to_path)
    packet = _projection_packet(binding, "c5_resource_profile", "c5_records_latest_unavailable")
    packet = dataclasses.replace(
        packet,
        units=(*packet.units, Unit(ref=f"{packet.anchors[0].ref}#unit", role="note", text="extra")),
    )

    score = score_case(packet, fixture_by_id("C5"), reference_binding=binding)

    assert score.gold_hit == 2
    assert score.precision == 0.8


def test_bound_scoring_keeps_irrelevant_padding_and_true_supersession_rules(product_corpus) -> None:
    root, manifest = product_corpus
    binding = freeze_reference_binding(root, manifest, manifest.key_to_path)
    packet = _projection_packet(binding, "c5_resource_profile", "c5_records_latest_unavailable")
    padded = dataclasses.replace(
        packet,
        units=(*packet.units, Unit(ref="irrelevant", role="note", text="extra")),
    )
    assert score_case(padded, fixture_by_id("C5"), reference_binding=binding).precision == 0.8

    c8 = fixture_by_id("C8")
    superseded = ActivationPacket(
        anchors=(
            Anchor(ref=manifest.key_to_path["c8_active_head"], title="head", kind="note", status="resolved"),
            Anchor(ref=manifest.key_to_path["c8_superseded_ancestor_1"], title="old", kind="note", status="resolved"),
        ),
        units=(
            Unit(ref=manifest.key_to_path["c8_active_head"], role="current_state", text="current"),
            Unit(
                ref=manifest.key_to_path["c8_superseded_ancestor_1"],
                role="current_state",
                text="old",
                lifecycle="superseded",
                provenance={"superseded_by": manifest.key_to_path["c8_active_head"]},
            ),
        ),
    )
    assert score_case(superseded, c8, reference_binding=binding).precision == 1.0


def _bound_manifest(manifest, binding, **changes):
    values = {
        "fixture_set_digest": "a" * 64,
        "corpus_digest": manifest.corpus_hash,
        "logical_corpus_digest": manifest.logical_hash,
        "threshold_digest": "c" * 64,
        "reference_binding_digest": binding.digest,
        "mechanism": "product",
    }
    values.update(changes)
    return validate_manifest(values)


@pytest.mark.parametrize("field", ["corpus_digest", "logical_corpus_digest", "reference_binding_digest"])
def test_product_run_refuses_manifest_binding_identity_mismatch(product_corpus, field: str) -> None:
    root, manifest = product_corpus
    binding = freeze_reference_binding(root, manifest, manifest.key_to_path)
    run_manifest = _bound_manifest(manifest, binding, **{field: "f" * 64})

    with pytest.raises(ManifestVoidError, match=field):
        run_audit({}, manifest=run_manifest, reference_binding=binding)


def test_product_run_refuses_missing_or_tampered_binding(product_corpus) -> None:
    root, manifest = product_corpus
    binding = freeze_reference_binding(root, manifest, manifest.key_to_path)

    with pytest.raises(ManifestVoidError, match="reference_binding"):
        run_audit({}, manifest=_bound_manifest(manifest, binding), reference_binding=None)
    without_digest = _bound_manifest(manifest, binding)
    without_digest = dataclasses.replace(without_digest, reference_binding_digest=None)
    with pytest.raises(ManifestVoidError, match="reference_binding_digest"):
        run_audit({}, manifest=without_digest, reference_binding=binding)
    with pytest.raises(ManifestVoidError, match="digest"):
        run_audit(
            {},
            manifest=_bound_manifest(manifest, binding),
            reference_binding=dataclasses.replace(binding, digest="f" * 64),
        )
    with pytest.raises(ManifestVoidError, match="digest"):
        run_audit(
            {},
            manifest=_bound_manifest(manifest, binding),
            reference_binding=dataclasses.replace(binding, corpus_digest="f" * 64),
        )


def test_product_run_refuses_mapping_tamper_without_a_new_binding_digest(product_corpus) -> None:
    root, manifest = product_corpus
    binding = freeze_reference_binding(root, manifest, manifest.key_to_path)
    mapping = dict(binding.key_to_ref)
    mapping["c5_resource_profile"] = "invented-ref"
    tampered = dataclasses.replace(binding, key_to_ref=tuple(sorted(mapping.items())))

    with pytest.raises(ManifestVoidError, match="digest"):
        run_audit({}, manifest=_bound_manifest(manifest, binding), reference_binding=tampered)


def test_freeze_refuses_a_stale_canonical_source_file(tmp_path: Path) -> None:
    manifest = build_corpus(tmp_path, distractor_count=0)
    source = tmp_path / manifest.key_to_path["t5_available_resource"]
    source.write_text(source.read_text(encoding="utf-8") + "\nchanged after manifest\n", encoding="utf-8")

    with pytest.raises(FixtureError, match="corpus digest"):
        freeze_reference_binding(tmp_path, manifest, manifest.key_to_path)
