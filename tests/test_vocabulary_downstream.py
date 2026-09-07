"""Public writer coverage for reviewed vocabulary applications downstream."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from test_entity_lifecycle_surface import _hydration
from test_vocabulary_projection import fixture_paged_origins
from test_vocabulary_review import payload

from exomem import (
    commands,
    entity_types,
    epistemic_graph,
    relation_registry,
    vocabulary_application,
    vocabulary_delivery,
    vocabulary_review,
    writer_lease,
)
from exomem.governance.principal import library_scope
from exomem.vault import content_hash
from exomem.vocabulary_state import VocabularyState
from exomem.vocabulary_workflow import Evidence, WorkItem, make_item


def _write(root: Path, path: str, content: str) -> str:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return hashlib.sha256(target.read_bytes()).hexdigest()


def _manager(tmp_path: Path) -> writer_lease.LeaseManager:
    return writer_lease.LeaseManager(
        writer_lease.LeaseConfig(state_dir=tmp_path / "writer-state")
    )


def _command(name: str):  # noqa: ANN201
    return next(entry for entry in commands.PRODUCT_COMMANDS if entry.name == name)


def _decision(item: WorkItem, *, outcome: str, choice: dict) -> dict:
    return {
        "item_ref": item.ref,
        "fingerprint": item.fingerprint,
        "family": item.family,
        "registry_hashes": dict(item.registry_hashes),
        "target_versions": dict(item.target_versions),
        "outcome": outcome,
        "choice": choice,
        "rationale": "The reviewed durable evidence establishes this exact meaning.",
    }


def _observe_and_decide(root: Path, item: WorkItem, *, outcome: str, choice: dict) -> None:
    store = VocabularyState(root)
    store.observe(item)
    with library_scope():
        store.decide(item, _decision(item, outcome=outcome, choice=choice), actor="owner")


def _item(
    root: Path,
    *,
    family: str,
    signal: str,
    source: str,
    targets: dict[str, str],
    logical_identity: str,
    projection_currency: dict[str, str] | None = None,
) -> WorkItem:
    source_version = content_hash((root / source).read_text(encoding="utf-8"))
    return make_item(
        family=family,
        signal=signal,
        targets=targets,
        evidence=[Evidence(source, source_version, "source:reviewed")],
        registry_hashes=vocabulary_review.registry_hashes(root),
        projection_status="current",
        paths={source: source, **{target: target for target in targets}},
        logical_identity=logical_identity,
        projection_currency=projection_currency,
    )


def test_custom_entity_type_is_registered_then_used_by_the_public_writer(tmp_path: Path) -> None:
    source = "Knowledge Base/Sources/Articles/venue-evidence.md"
    source_version = _write(
        tmp_path,
        source,
        "---\ntype: source\n---\nA venue is a stable location for recurring events.\n",
    )
    definition = {
        "folder": "Venues",
        "label": "Venue",
        "aliases": ["event venue"],
        "capture_guidance": "Capture a stable location used for recurring events.",
        "status": "active",
        "parent": "concept",
    }
    type_item = _item(
        tmp_path,
        family="entity-type/v1",
        signal="venue-type",
        source=source,
        targets={source: source_version},
        logical_identity="meaning:venue",
    )
    _observe_and_decide(
        tmp_path,
        type_item,
        outcome="propose-new",
        choice={"canonical": "venue", "definition": definition},
    )
    manager = _manager(tmp_path)
    proposal = {"schema_version": 1, "entity_types": {"venue": definition}}
    with library_scope():
        registered = manager.invoke(
            _command("schema_memory"),
            (tmp_path,),
            {
                "operation": "save-entity-types",
                "proposal": proposal,
                "expected_hash": entity_types.load_entity_types(tmp_path).extension_hash,
                "why": "Register the reviewed venue entity type.",
                "vocabulary_ref": type_item.ref,
                "vocabulary_fingerprint": type_item.fingerprint,
            },
            idempotency_key="venue-type-registration",
            read_only=False,
        )

    assert registered["state"] == "committed"
    assert registered["receipt_id"]
    assert VocabularyState(tmp_path).get(type_item.ref)["state"] == "applied"
    resolved_type = commands.op_schema_memory(
        tmp_path,
        subject="entity-types",
        operation="resolve-entity-type",
        requested_type="event venue",
    )
    assert resolved_type["exact_matches"] == [
        {
            "match": "alias",
            "requested_type": "event venue",
            "canonical": "venue",
            "id": "venue",
            "folder": "Venues",
            "label": "Venue",
            "aliases": ["event venue"],
            "description": definition["capture_guidance"],
            "optional_frontmatter": [],
            "cue_nouns": ["event venue"],
            "parent": "concept",
            "status": "active",
            "replaced_by": None,
            "core": False,
        }
    ]

    entity_item = _item(
        tmp_path,
        family="entity-instance/v1",
        signal="cedar-hall",
        source=source,
        targets={source: source_version},
        logical_identity="entity:cedar-hall",
        projection_currency={"candidate_state": "promotion"},
    )
    entity_definition = {
        "entity_type": "venue",
        "name": "Cedar Hall",
        "summary": "A stable venue for recurring community events.",
    }
    _observe_and_decide(
        tmp_path,
        entity_item,
        outcome="propose-new",
        choice={"canonical": "Cedar Hall", "definition": entity_definition},
    )
    with library_scope():
        created = manager.invoke(
            _command("connect_memory"),
            (tmp_path,),
            {
                "operation": "create-entity",
                **entity_definition,
                "vocabulary_ref": entity_item.ref,
                "vocabulary_fingerprint": entity_item.fingerprint,
            },
            idempotency_key="cedar-hall-creation",
            read_only=False,
        )

    assert created["state"] == "committed"
    assert created["receipt_id"]
    assert VocabularyState(tmp_path).get(entity_item.ref)["state"] == "applied"
    exact = commands.op_connect_memory(
        tmp_path, operation="resolve-entity", name="Cedar Hall", entity_type="venue"
    )
    assert exact["status"] == "match"
    assert exact["candidates"][0]["path"] == "Knowledge Base/Entities/Venues/Cedar Hall.md"


def test_registry_change_since_review_refuses_the_stale_type_application(tmp_path: Path) -> None:
    source = "Knowledge Base/Sources/Articles/venue-evidence.md"
    source_version = _write(tmp_path, source, "---\ntype: source\n---\nA stable location.\n")
    venue = {
        "folder": "Venues",
        "label": "Venue",
        "aliases": [],
        "capture_guidance": "Capture a stable location.",
        "status": "active",
        "parent": "concept",
    }
    other = {**venue, "folder": "Tools", "label": "Tool"}
    item = _item(
        tmp_path,
        family="entity-type/v1",
        signal="venue-type",
        source=source,
        targets={source: source_version},
        logical_identity="meaning:venue",
    )
    _observe_and_decide(
        tmp_path, item, outcome="propose-new", choice={"canonical": "venue", "definition": venue}
    )
    with library_scope():
        entity_types.save_registry(
            tmp_path,
            {"schema_version": 1, "entity_types": {"tool": other}},
            expected_hash=None,
            observed_ids=(),
        )
    manager = _manager(tmp_path)
    with library_scope(), pytest.raises(ValueError, match="VOCABULARY_DECISION_STALE"):
        manager.invoke(
            _command("schema_memory"),
            (tmp_path,),
            {
                "operation": "save-entity-types",
                "proposal": {"schema_version": 1, "entity_types": {"tool": other, "venue": venue}},
                "expected_hash": entity_types.load_entity_types(tmp_path).extension_hash,
                "why": "Apply the reviewed venue type.",
                "vocabulary_ref": item.ref,
                "vocabulary_fingerprint": item.fingerprint,
            },
            idempotency_key="stale-reviewed-application",
            read_only=False,
        )
    assert entity_types.load_entity_types(tmp_path).resolve("venue") is None
    assert VocabularyState(tmp_path).get(item.ref)["decision_currency"] == "refresh_required"


def test_reuse_type_correlation_cannot_apply_an_unrelated_registry_save(tmp_path: Path) -> None:
    source = "Knowledge Base/Sources/Articles/type-evidence.md"
    source_version = _write(tmp_path, source, "---\ntype: source\n---\nA type already exists.\n")
    item = _item(
        tmp_path,
        family="entity-type/v1",
        signal="organization-type",
        source=source,
        targets={source: source_version},
        logical_identity="meaning:organization",
    )
    _observe_and_decide(
        tmp_path, item, outcome="reuse", choice={"canonical": "organization"}
    )
    with library_scope(), pytest.raises(ValueError, match="VOCABULARY_APPLICATION_INVALID"):
        _manager(tmp_path).invoke(
            _command("schema_memory"),
            (tmp_path,),
            {
                "operation": "save-entity-types",
                "proposal": {"schema_version": 1, "entity_types": {}},
                "expected_hash": None,
                "why": "Save an unrelated registry proposal.",
                "vocabulary_ref": item.ref,
                "vocabulary_fingerprint": item.fingerprint,
            },
            idempotency_key="unrelated-type-save",
            read_only=False,
        )
    assert VocabularyState(tmp_path).get(item.ref)["state"] == "proposed"


def test_committed_registry_terminal_recovers_an_uncertain_application(tmp_path: Path, monkeypatch) -> None:
    source = "Knowledge Base/Sources/Articles/venue-evidence.md"
    source_version = _write(tmp_path, source, "---\ntype: source\n---\nA stable location.\n")
    definition = {
        "folder": "Venues",
        "label": "Venue",
        "aliases": [],
        "capture_guidance": "Capture a stable location.",
        "status": "active",
        "parent": "concept",
    }
    item = _item(
        tmp_path,
        family="entity-type/v1",
        signal="venue-type",
        source=source,
        targets={source: source_version},
        logical_identity="meaning:venue",
    )
    _observe_and_decide(
        tmp_path, item, outcome="propose-new", choice={"canonical": "venue", "definition": definition}
    )
    arguments = {
        "operation": "save-entity-types",
        "proposal": {"schema_version": 1, "entity_types": {"venue": definition}},
        "expected_hash": entity_types.load_entity_types(tmp_path).extension_hash,
        "why": "Register the reviewed venue type.",
        "vocabulary_ref": item.ref,
        "vocabulary_fingerprint": item.fingerprint,
    }
    manager = _manager(tmp_path)
    real_commit = vocabulary_application.commit
    monkeypatch.setattr(
        vocabulary_application,
        "commit",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("review state unavailable")),
    )
    with library_scope():
        first = manager.invoke(
            _command("schema_memory"), (tmp_path,), arguments, idempotency_key="uncertain-venue", read_only=False
        )
    assert first["state"] == "committed"
    assert VocabularyState(tmp_path).get(item.ref)["decision"]["application"]["state"] == "uncertain"
    monkeypatch.setattr(vocabulary_application, "commit", real_commit)
    with library_scope():
        replay = manager.invoke(
            _command("schema_memory"), (tmp_path,), arguments, idempotency_key="uncertain-venue", read_only=False
        )
    assert replay == first
    assert VocabularyState(tmp_path).get(item.ref)["state"] == "applied"


def test_completed_registry_terminal_recovers_when_outage_left_the_operation_bound(
    tmp_path: Path, monkeypatch
) -> None:
    source = "Knowledge Base/Sources/Articles/venue-evidence.md"
    source_version = _write(tmp_path, source, "---\ntype: source\n---\nA stable location.\n")
    definition = {
        "folder": "Venues",
        "label": "Venue",
        "aliases": [],
        "capture_guidance": "Capture a stable location.",
        "status": "active",
        "parent": "concept",
    }
    item = _item(
        tmp_path,
        family="entity-type/v1",
        signal="venue-type",
        source=source,
        targets={source: source_version},
        logical_identity="meaning:venue",
    )
    _observe_and_decide(
        tmp_path, item, outcome="propose-new", choice={"canonical": "venue", "definition": definition}
    )
    arguments = {
        "operation": "save-entity-types",
        "proposal": {"schema_version": 1, "entity_types": {"venue": definition}},
        "expected_hash": entity_types.load_entity_types(tmp_path).extension_hash,
        "why": "Register the reviewed venue type.",
        "vocabulary_ref": item.ref,
        "vocabulary_fingerprint": item.fingerprint,
    }
    manager = _manager(tmp_path)
    real_commit = vocabulary_application.commit
    real_uncertain = VocabularyState.record_application_uncertain
    monkeypatch.setattr(
        vocabulary_application,
        "commit",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("review state unavailable")),
    )
    monkeypatch.setattr(
        VocabularyState,
        "record_application_uncertain",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("review state unavailable")),
    )
    with library_scope():
        first = manager.invoke(
            _command("schema_memory"), (tmp_path,), arguments, idempotency_key="bound-venue", read_only=False
        )
    assert first["state"] == "committed"
    assert VocabularyState(tmp_path).get(item.ref)["decision"]["application"].get("state") is None
    monkeypatch.setattr(vocabulary_application, "commit", real_commit)
    monkeypatch.setattr(VocabularyState, "record_application_uncertain", real_uncertain)
    with library_scope():
        replay = manager.invoke(
            _command("schema_memory"), (tmp_path,), arguments, idempotency_key="bound-venue", read_only=False
        )
    assert replay == first
    assert VocabularyState(tmp_path).get(item.ref)["state"] == "applied"


def test_relation_registration_preserves_history_and_binds_an_authored_edge(tmp_path: Path) -> None:
    source = "Knowledge Base/Notes/venue.md"
    target = "Knowledge Base/Notes/event.md"
    source_version = _write(
        tmp_path,
        source,
        "---\ntype: insight\nstatus: active\n---\n# Venue\n\nSee [[Knowledge Base/Notes/event]].\n",
    )
    target_version = _write(
        tmp_path,
        target,
        "---\ntype: insight\nstatus: active\n---\n# Event\n\nA recurring community event.\n",
    )
    extension = {
        "parent": "relates_to",
        "description": "A venue hosts a recurring event.",
        "direction": "directed",
        "aliases": ["hosts"],
        "origins": ["markdown_relation", "semantic_relation"],
    }
    relation_item = _item(
        tmp_path,
        family="relation-type/v1",
        signal="venue-hosts",
        source=source,
        targets={source: source_version, target: target_version},
        logical_identity="meaning:venue.hosts",
    )
    _observe_and_decide(
        tmp_path,
        relation_item,
        outcome="propose-new",
        choice={"canonical": "venue.hosts", "definition": extension},
    )
    manager = _manager(tmp_path)
    with library_scope():
        registered = manager.invoke(
            _command("schema_memory"),
            (tmp_path,),
            {
                "subject": "relations",
                "operation": "save-relations",
                "proposal": {"upsert": {"venue.hosts": extension}},
                "expected_hash": relation_registry.load_registry(tmp_path).extension_hash,
                "why": "Register the reviewed venue-to-event meaning.",
                "vocabulary_ref": relation_item.ref,
                "vocabulary_fingerprint": relation_item.fingerprint,
            },
            idempotency_key="venue-hosts-registration",
            read_only=False,
        )

    assert registered["state"] == "committed"
    pending = VocabularyState(tmp_path).get(relation_item.ref)
    assert pending["state"] == "applying"
    assert pending["decision"]["application"]["operations"][0]["state"] == "committed"
    assert len(pending["receipts"]) == 1
    family = commands.op_connect_memory(
        tmp_path,
        operation="resolve-relation",
        requested_relation="hosts",
    )
    assert family["exact_matches"][0]["canonical"] == "venue.hosts"
    assert family["exact_matches"][0]["parent"] == "relates_to"

    with library_scope():
        epistemic_graph.EpistemicGraphIndex(tmp_path).rebuild_all()
    queue = commands.op_review_memory(tmp_path, mode="relation-queue")
    candidate = next(
        item
        for group in queue["groups"]
        for item in group["items"]
        if item["from"] == source and item["to"] == target
    )
    with library_scope(), pytest.raises(
        ValueError, match="VOCABULARY_APPLICATION_INVALID"
    ):
        manager.invoke(
            _command("connect_memory"),
            (tmp_path,),
            {
                "operation": "accept-relation",
                "ref": candidate["ref"],
                "expected_hash": candidate["source_content_hash"],
                "expected_fingerprint": candidate["fingerprint"],
                "why": "A generic detector result cannot replace the reviewed meaning.",
                "vocabulary_ref": relation_item.ref,
                "vocabulary_fingerprint": relation_item.fingerprint,
            },
            idempotency_key="venue-event-generic-edge",
            read_only=False,
        )
    assert VocabularyState(tmp_path).get(relation_item.ref)["state"] == "applying"
    edge_kwargs = {
        "operation": "accept-relation",
        "ref": candidate["ref"],
        "requested_relation": "hosts",
        "expected_hash": candidate["source_content_hash"],
        "expected_fingerprint": candidate["fingerprint"],
        "why": "Author the reviewed venue-to-event edge.",
        "vocabulary_ref": relation_item.ref,
        "vocabulary_fingerprint": relation_item.fingerprint,
    }
    with library_scope():
        accepted = manager.invoke(
            _command("connect_memory"),
            (tmp_path,),
            edge_kwargs,
            idempotency_key="venue-event-edge",
            read_only=False,
        )

    assert accepted["state"] == "committed"
    applied = VocabularyState(tmp_path).get(relation_item.ref)
    assert applied["state"] == "applied"
    assert len(applied["receipts"]) == 2
    assert "- venue.hosts [[Knowledge Base/Notes/event]]" in (tmp_path / source).read_text(
        encoding="utf-8"
    )
    with library_scope():
        replay = manager.invoke(
            _command("connect_memory"),
            (tmp_path,),
            edge_kwargs,
            idempotency_key="venue-event-edge",
            read_only=False,
        )
    assert replay == accepted
    assert (tmp_path / source).read_text(encoding="utf-8").count("venue.hosts") == 1
    with library_scope():
        epistemic_graph.EpistemicGraphIndex(tmp_path).rebuild_all()
    graph = commands.op_graph_context(
        tmp_path,
        path=source,
        relation_types=["venue.hosts"],
    )
    assert graph["edges"], graph
    assert graph["edges"][0]["relation_type"] == "venue.hosts"

    deprecated = manager.invoke(
        _command("schema_memory"),
        (tmp_path,),
        {
            "subject": "relations",
            "operation": "save-relations",
            "proposal": {"deprecate": {"venue.hosts": "relates_to"}},
            "expected_hash": relation_registry.load_registry(tmp_path).extension_hash,
            "why": "Retire the old name while retaining its history.",
        },
        idempotency_key="venue-hosts-deprecation",
        read_only=False,
    )
    assert deprecated["state"] == "committed"
    historical = commands.op_connect_memory(
        tmp_path, operation="resolve-relation", requested_relation="venue.hosts"
    )
    assert historical["exact_matches"][0]["status"] == "deprecated"
    assert historical["exact_matches"][0]["parent"] == "relates_to"


def test_selected_relation_refuses_without_a_vocabulary_binding(tmp_path: Path) -> None:
    source = "Knowledge Base/Notes/venue.md"
    target = "Knowledge Base/Notes/event.md"
    _write(
        tmp_path,
        source,
        "---\ntype: insight\nstatus: active\n---\n# Venue\n\nSee [[Knowledge Base/Notes/event]].\n",
    )
    _write(
        tmp_path,
        target,
        "---\ntype: insight\nstatus: active\n---\n# Event\n\nA recurring community event.\n",
    )
    with library_scope():
        relation_registry.save_registry(
            tmp_path,
            {
                "schema_version": 1,
                "extensions": {
                    "venue.hosts": {
                        "parent": "relates_to",
                        "description": "A venue hosts a recurring event.",
                        "direction": "directed",
                        "origins": ["markdown_relation", "semantic_relation"],
                    }
                },
            },
        )
        epistemic_graph.EpistemicGraphIndex(tmp_path).rebuild_all()
    queue = commands.op_review_memory(tmp_path, mode="relation-queue")
    candidate = next(
        item
        for group in queue["groups"]
        for item in group["items"]
        if item["from"] == source and item["to"] == target
    )

    with library_scope(), pytest.raises(
        ValueError, match="VOCABULARY_APPLICATION_INVALID"
    ):
        commands.op_connect_memory(
            tmp_path,
            operation="accept-relation",
            ref=candidate["ref"],
            requested_relation="venue.hosts",
            expected_hash=candidate["source_content_hash"],
            expected_fingerprint=candidate["fingerprint"],
            why="This semantic choice needs its reviewed vocabulary binding.",
        )
    assert "## Relations" not in (tmp_path / source).read_text(encoding="utf-8")


def test_curation_resume_keeps_a_vocabulary_application_open_until_all_steps_commit(
    tmp_path: Path,
) -> None:
    target = _hydration(tmp_path, identity="cobalt workshop")
    report = commands.op_review_memory(
        tmp_path, categories=["entity_recurrence"], limit=3
    )
    route = report["items"][0]["vocabulary"]
    item = VocabularyState(tmp_path).get(route["ref"])
    decision = payload(item, "enrich") | {"choice": {"canonical": target}}
    with library_scope():
        from exomem import vocabulary_review as review

        review.decide(tmp_path, ref=item["ref"], decision=decision)
    current = VocabularyState(tmp_path).get(item["ref"])
    binding = commands.op_maintain_memory(
        tmp_path,
        mode="curation",
        curation_action="work-item",
        review_ref=current["logical_identity"],
    )["entity_candidate"]
    assert binding["review_ref"] == current["logical_identity"]
    assert len(binding["review_fingerprint"]) == 24
    assert len(current["fingerprint"]) == 64
    contexts = binding["first_disconnected_context_batch"][:2]
    assert len(contexts) == 2
    steps = []
    for ordinal, context in enumerate(contexts):
        path = context["path"]
        before = (tmp_path / path).read_text(encoding="utf-8")
        steps.append(
            {
                "step_id": f"connect-{ordinal}",
                "kind": "edit",
                "args": {
                    "path": path,
                    "why": "Connect one exact reviewed hydration context.",
                    "operation": {
                        "kind": "replace_string",
                        "old_string": binding["identity"],
                        "new_string": f"[[{target.removesuffix('.md')}|{binding['identity']}]]",
                        "expected_hash": content_hash(before),
                    },
                },
            }
        )
    plan = {
        "version": 1,
        "title": "Connect two reviewed hydration contexts",
        "entity_candidate": binding,
        "steps": steps,
    }
    proposed = commands.op_maintain_memory(
        tmp_path, mode="curation", curation_action="propose", plan=plan
    )
    manager = _manager(tmp_path)
    apply_arguments = {
        "mode": "curation",
        "curation_action": "apply",
        "run_id": proposed["run_id"],
        "plan_id": proposed["plan_id"],
        "expected_plan_fingerprint": proposed["plan_fingerprint"],
        "why": "Apply the first exact reviewed hydration step.",
        "vocabulary_ref": current["ref"],
        "vocabulary_fingerprint": current["fingerprint"],
    }
    with library_scope():
        first = manager.invoke(
            _command("maintain_memory"),
            (tmp_path,),
            apply_arguments,
            idempotency_key="hydration-apply",
            read_only=False,
        )

    assert first["state"] == "committed"
    assert first.get("leaf_result", first)["phase"] == "executing"
    assert VocabularyState(tmp_path).get(current["ref"])["state"] == "applying"
    resume_arguments = {
        "mode": "curation",
        "curation_action": "resume",
        "run_id": proposed["run_id"],
        "plan_id": proposed["plan_id"],
        "vocabulary_ref": current["ref"],
        "vocabulary_fingerprint": current["fingerprint"],
    }
    with library_scope():
        resumed = manager.invoke(
            _command("maintain_memory"),
            (tmp_path,),
            resume_arguments,
            idempotency_key="hydration-resume",
            read_only=False,
        )

    assert resumed["state"] == "committed"
    assert resumed.get("leaf_result", resumed)["phase"] == "completed"
    assert VocabularyState(tmp_path).get(current["ref"])["state"] == "applied"
    with library_scope():
        replay = manager.invoke(
            _command("maintain_memory"),
            (tmp_path,),
            resume_arguments,
            idempotency_key="hydration-resume",
            read_only=False,
        )
    assert replay == resumed


def test_public_terminal_publishes_warming_then_current_vocabulary_work(
    tmp_path: Path,
) -> None:
    path = fixture_paged_origins(tmp_path, independent_at=11)
    before = (tmp_path / path).read_text(encoding="utf-8")
    manager = _manager(tmp_path)
    with library_scope():
        terminal = manager.invoke(
            _command("edit_memory"),
            (tmp_path,),
            {
                "path": path,
                "why": "Record the reviewed publication update.",
                "operation": {
                    "kind": "replace_string",
                    "old_string": "## Relations\n- relates_to [[Entities/Organizations/second]]",
                    "new_string": (
                        "## Relations\n- relates_to [[Entities/Organizations/second]]"
                        "\n\nPublication refreshed."
                    ),
                    "expected_hash": content_hash(before),
                },
            },
            idempotency_key="vocabulary-publication-warming",
            read_only=False,
        )

    assert terminal["state"] == "committed"
    assert terminal["vocabulary_sync"]["state"] == "unavailable"
    with library_scope():
        epistemic_graph.EpistemicGraphIndex(tmp_path).rebuild_all()
        warming = vocabulary_delivery.recover(tmp_path)
        recovered = vocabulary_delivery.recover(tmp_path)
    assert warming == {"state": "warming", "processed": 1, "coverage": "bounded-pass"}
    assert recovered == {"state": "current", "processed": 1, "coverage": "bounded-pass"}
    assert VocabularyState(tmp_path).page()["items"]
