import hashlib
import uuid
from types import SimpleNamespace

import pytest

from exomem import (
    memory_refs,
    vocabulary_application,
    vocabulary_authority,
    vocabulary_gate,
    vocabulary_review,
    writer_lease,
)
from exomem.governance.principal import library_scope
from exomem.vocabulary_effects import Effect
from exomem.vocabulary_state import VocabularyState
from exomem.vocabulary_workflow import Evidence, make_item


def _page(root, path, body):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body)
    return hashlib.sha256(target.read_bytes()).hexdigest()


def _item(vault):
    source = "Knowledge Base/Sources/Articles/source.md"
    target = "Knowledge Base/Entities/Organizations/acme.md"
    versions = {
        source: _page(vault, source, "---\ntype: source\n---\nIndependent evidence.\n"),
        target: _page(vault, target, "---\ntype: entity\nentity_type: organization\n---\nAcme.\n"),
    }
    return make_item(
        family="entity-instance/v1",
        signal="test-entity-lifecycle",
        targets={target: versions[target]},
        evidence=[Evidence(source, versions[source], "source:one")],
        registry_hashes=vocabulary_review.registry_hashes(vault),
        projection_status="current",
        logical_identity="review:acme",
        projection_currency={"candidate_state": "promotion"},
    )


def _decision(item):
    return {
        "item_ref": item.ref,
        "fingerprint": item.fingerprint,
        "family": item.family,
        "registry_hashes": dict(item.registry_hashes),
        "target_versions": dict(item.target_versions),
        "outcome": "propose-new",
        "choice": {
            "canonical": "Acme Labs",
            "definition": {
                "entity_type": "organization",
                "name": "Acme Labs",
                "summary": "A durable organization identity.",
            },
        },
        "rationale": "Recurring sources identify the same durable organization.",
    }


def _proposed(vault):
    item = _item(vault)
    store = VocabularyState(vault)
    store.observe(item)
    with library_scope():
        store.decide(item, _decision(item), actor="owner")
    return item


def _proposed_relation(vault):
    source = "Knowledge Base/Notes/source.md"
    target = "Knowledge Base/Notes/target.md"
    versions = {
        source: _page(vault, source, "---\ntype: insight\n---\nSource.\n"),
        target: _page(vault, target, "---\ntype: insight\n---\nTarget.\n"),
    }
    item = make_item(
        family="relation-type/v1",
        signal="generic-pair",
        targets={source: versions[source], target: versions[target]},
        evidence=[Evidence(source, versions[source], "source:one")],
        registry_hashes=vocabulary_review.registry_hashes(vault),
        projection_status="current",
    )
    decision = {
        "item_ref": item.ref, "fingerprint": item.fingerprint, "family": item.family,
        "registry_hashes": dict(item.registry_hashes), "target_versions": dict(item.target_versions),
        "outcome": "reuse", "choice": {"canonical": "part_of"},
        "rationale": "The existing canonical relation states this exact edge.",
    }
    store = VocabularyState(vault)
    store.observe(item)
    with library_scope():
        store.decide(item, decision, actor="owner")
    return item, source, target


def test_binding_refuses_command_that_does_not_match_reviewed_choice(tmp_path):
    item = _proposed(tmp_path)
    with pytest.raises(ValueError, match="VOCABULARY_APPLICATION_INVALID"):
        vocabulary_application.bind(
            tmp_path,
            command="connect_memory",
            kwargs={
                "operation": "create-entity",
                "entity_type": "organization",
                "name": "Other Labs",
                "summary": "A durable organization identity.",
                "vocabulary_ref": item.ref,
                "vocabulary_fingerprint": item.fingerprint,
            },
            idempotency_key="write-1",
            command_digest="a" * 64,
            principal="owner",
        )


def test_relation_registry_mismatch_names_implicit_aliases_without_mutating(tmp_path):
    source = "Knowledge Base/Notes/source.md"
    target = "Knowledge Base/Notes/target.md"
    targets = {
        source: _page(tmp_path, source, "---\ntype: insight\n---\nSource.\n"),
        target: _page(tmp_path, target, "---\ntype: insight\n---\nTarget.\n"),
    }
    reviewed = {
        "parent": "relates_to",
        "description": "A commerce source supplies goods to a target.",
        "direction": "directed",
        "aliases": [],
        "origins": ["semantic_relation"],
    }
    item = make_item(
        family="relation-type/v1",
        signal="commerce-supplies-goods-to",
        targets=targets,
        evidence=[Evidence(source, targets[source], "source:one")],
        registry_hashes=vocabulary_review.registry_hashes(tmp_path),
        projection_status="current",
        logical_identity="meaning:commerce.supplies_goods_to",
    )
    store = VocabularyState(tmp_path)
    store.observe(item)
    with library_scope():
        store.decide(
            item,
            {
                "item_ref": item.ref,
                "fingerprint": item.fingerprint,
                "family": item.family,
                "registry_hashes": dict(item.registry_hashes),
                "target_versions": dict(item.target_versions),
                "outcome": "propose-new",
                "choice": {"canonical": "commerce.supplies_goods_to", "definition": reviewed},
                "rationale": "The reviewed pair establishes this exact relation.",
            },
            actor="owner",
        )
    with pytest.raises(ValueError, match="fields aliases; record the exact returned delta.upsert"):
        vocabulary_application.bind(
            tmp_path,
            command="schema_memory",
            kwargs={
                "subject": "relations",
                "operation": "save-relations",
                "proposal": {
                    "upsert": {
                        "commerce.supplies_goods_to": reviewed | {"aliases": ["supplies_goods_to"]}
                    }
                },
                "vocabulary_ref": item.ref,
                "vocabulary_fingerprint": item.fingerprint,
            },
            idempotency_key="commerce-supplies-goods-to",
            command_digest="a" * 64,
            principal="owner",
        )
    assert VocabularyState(tmp_path).get(item.ref)["state"] == "proposed"


def test_enrich_choice_refuses_a_correlated_create_entity_without_crashing(tmp_path):
    item = _item(tmp_path)
    store = VocabularyState(tmp_path)
    store.observe(item)
    decision = _decision(item) | {
        "outcome": "enrich",
        "choice": {"canonical": "Knowledge Base/Entities/Organizations/acme.md"},
    }
    with library_scope():
        store.decide(item, decision, actor="owner")
    with pytest.raises(ValueError, match="VOCABULARY_APPLICATION_INVALID"):
        vocabulary_application.bind(
            tmp_path,
            command="connect_memory",
            kwargs={
                "operation": "create-entity",
                "entity_type": "organization",
                "name": "Other",
                "summary": "Other summary.",
                "vocabulary_ref": item.ref,
                "vocabulary_fingerprint": item.fingerprint,
            },
            idempotency_key="enrich-create-mismatch",
            command_digest="e" * 64,
            principal="owner",
        )
    assert VocabularyState(tmp_path).get(item.ref)["state"] == "proposed"


def test_bound_operation_only_commits_from_writer_sealed_terminal(tmp_path):
    item = _proposed(tmp_path)
    binding = vocabulary_application.bind(
        tmp_path,
        command="connect_memory",
        kwargs={
            "operation": "create-entity",
            "entity_type": "organization",
            "name": "Acme Labs",
            "summary": "A durable organization identity.",
            "vocabulary_ref": item.ref,
            "vocabulary_fingerprint": item.fingerprint,
        },
        idempotency_key="write-1",
        command_digest="a" * 64,
        principal="owner",
    )
    with pytest.raises(ValueError, match="VOCABULARY_APPLICATION_INVALID"):
        vocabulary_application.commit(tmp_path, binding, {"state": "committed"})
    assert VocabularyState(tmp_path).get(item.ref)["state"] == "applying"


def test_accept_relation_must_recheck_the_current_candidate_and_reviewed_pair(tmp_path, monkeypatch):
    item, source, target = _proposed_relation(tmp_path)
    from exomem import relation_queue

    candidate = type("Candidate", (), {
        "fingerprint": "f" * 24,
        "candidate": {"from": source, "to": target, "relation_type": "part_of"},
    })()
    monkeypatch.setattr(relation_queue, "resolve_candidate", lambda *_args, **_kwargs: candidate)
    binding = vocabulary_application.bind(
        tmp_path,
        command="connect_memory",
        kwargs={
            "operation": "accept-relation", "ref": "exomem://review/relation/" + "a" * 24,
            "expected_fingerprint": "f" * 24, "why": "Apply the reviewed canonical edge.",
            "vocabulary_ref": item.ref, "vocabulary_fingerprint": item.fingerprint,
        },
        idempotency_key="edge-1", command_digest="b" * 64, principal="owner",
    )
    assert binding.item.ref == item.ref
    with pytest.raises(ValueError, match="VOCABULARY_APPLICATION_INVALID"):
        vocabulary_application.bind(
            tmp_path,
            command="connect_memory",
            kwargs={
                "operation": "accept-relation", "ref": "exomem://review/relation/" + "a" * 24,
                "expected_fingerprint": "not-current", "why": "Apply the reviewed canonical edge.",
                "vocabulary_ref": item.ref, "vocabulary_fingerprint": item.fingerprint,
            },
            idempotency_key="edge-2", command_digest="c" * 64, principal="owner",
        )


def test_explicit_paired_question_binds_the_directed_candidate(tmp_path, monkeypatch):
    source = "Knowledge Base/Notes/source.md"
    target = "Knowledge Base/Notes/target.md"
    versions = {
        source: _page(tmp_path, source, "---\ntype: insight\n---\nSource.\n"),
        target: _page(tmp_path, target, "---\ntype: insight\n---\nTarget.\n"),
    }
    ref = "exomem://review/relation/" + "a" * 24
    item = make_item(
        family="relation-type/v1",
        signal="agent-meaning-question",
        targets=versions,
        evidence=[Evidence(source, versions[source], "agent-meaning-question")],
        registry_hashes=vocabulary_review.registry_hashes(tmp_path),
        projection_status="current",
        paths={source: source, target: target},
        question="Does this reviewed relation need a more precise meaning?",
        projection_currency={
            "candidate_ref": ref,
            "candidate_fingerprint": "f" * 24,
            "candidate_source_path": source,
            "candidate_target_path": target,
        },
    )
    store = VocabularyState(tmp_path)
    store.observe(item)
    decision = {
        "item_ref": item.ref,
        "fingerprint": item.fingerprint,
        "family": item.family,
        "registry_hashes": dict(item.registry_hashes),
        "target_versions": dict(item.target_versions),
        "outcome": "reuse",
        "choice": {"canonical": "part_of"},
        "rationale": "The reviewed directed candidate uses the existing relation.",
    }
    with library_scope():
        store.decide(item, decision, actor="owner")
    from exomem import relation_queue

    candidate = type("Candidate", (), {
        "fingerprint": "f" * 24,
        "candidate": {"from": source, "to": target, "relation_type": "part_of"},
    })()
    monkeypatch.setattr(relation_queue, "resolve_candidate", lambda *_args, **_kwargs: candidate)
    kwargs = {
        "operation": "accept-relation",
        "ref": ref,
        "path": source,
        "expected_fingerprint": "f" * 24,
        "why": "Apply the reviewed directed edge.",
        "vocabulary_ref": item.ref,
        "vocabulary_fingerprint": item.fingerprint,
    }
    assert vocabulary_application.bind(
        tmp_path,
        command="connect_memory",
        kwargs=kwargs,
        idempotency_key="paired-edge",
        command_digest="d" * 64,
        principal="owner",
    ).item.ref == item.ref

    with pytest.raises(ValueError, match="VOCABULARY_APPLICATION_INVALID"):
        vocabulary_application.bind(
            tmp_path,
            command="connect_memory",
            kwargs=kwargs | {"ref": "exomem://review/relation/" + "b" * 24},
            idempotency_key="different-candidate",
            command_digest="e" * 64,
            principal="owner",
        )
    candidate.candidate = {"from": target, "to": source, "relation_type": "part_of"}
    with pytest.raises(ValueError, match="VOCABULARY_APPLICATION_INVALID"):
        vocabulary_application.bind(
            tmp_path,
            command="connect_memory",
            kwargs=kwargs,
            idempotency_key="reversed-candidate",
            command_digest="f" * 64,
            principal="owner",
        )


def test_lease_commits_a_reviewed_edge_only_after_the_canonical_source_changes(tmp_path, monkeypatch):
    item, source, target = _proposed_relation(tmp_path)
    from exomem import relation_queue

    candidate = type("Candidate", (), {
        "fingerprint": "f" * 24,
        "candidate": {"from": source, "to": target, "relation_type": "part_of"},
    })()
    monkeypatch.setattr(relation_queue, "resolve_candidate", lambda *_args, **_kwargs: candidate)
    calls = 0

    def leaf(vault, **kwargs):
        nonlocal calls
        calls += 1
        (vault / source).write_text(
            "---\ntype: insight\n---\nSource.\n\n## Relations\n\n- part_of [[Knowledge Base/Notes/target]]\n"
        )
        writer_lease.mark_active_mutation_committed()
        return {"from": source, "to": target, "relation_type": "part_of", "path": source}

    command = SimpleNamespace(name="connect_memory", leaf=leaf, read_only=False)
    manager = writer_lease.LeaseManager(writer_lease.LeaseConfig(state_dir=tmp_path / "lease"))
    kwargs = {
        "operation": "accept-relation", "ref": "exomem://review/relation/" + "a" * 24,
        "expected_fingerprint": "f" * 24, "why": "Apply the reviewed canonical edge.",
        "vocabulary_ref": item.ref, "vocabulary_fingerprint": item.fingerprint,
    }
    with library_scope():
        first = manager.invoke(command, (tmp_path,), kwargs, idempotency_key="edge-replay")
        replay = manager.invoke(command, (tmp_path,), kwargs, idempotency_key="edge-replay")
    assert calls == 1
    assert replay == first
    assert VocabularyState(tmp_path).get(item.ref)["state"] == "applied"


def test_lease_commits_one_matching_reviewed_application_and_replays_without_leaf(tmp_path):
    item = _proposed(tmp_path)
    calls = 0

    def leaf(vault, **kwargs):
        nonlocal calls
        calls += 1
        path = vault / "Knowledge Base/Entities/Organizations/acme-labs.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "---\ntype: entity\nentity_type: organization\ntitle: Acme Labs\n---\nA durable organization identity.\n"
        )
        writer_lease.mark_active_mutation_committed()
        return {"path": str(path.relative_to(vault))}

    command = SimpleNamespace(name="connect_memory", leaf=leaf, read_only=False)
    manager = writer_lease.LeaseManager(writer_lease.LeaseConfig(state_dir=tmp_path / "lease"))
    kwargs = {
        "operation": "create-entity",
        "entity_type": "organization",
        "name": "Acme Labs",
        "summary": "A durable organization identity.",
        "vocabulary_ref": item.ref,
        "vocabulary_fingerprint": item.fingerprint,
    }
    with library_scope():
        first = manager.invoke(command, (tmp_path,), kwargs, idempotency_key="reviewed-write")
        replay = manager.invoke(command, (tmp_path,), kwargs, idempotency_key="reviewed-write")
    assert calls == 1
    assert replay == first
    applied = VocabularyState(tmp_path).get(item.ref)
    assert applied["state"] == "applied"
    assert len(applied["receipts"]) == 1


def test_retry_after_a_precommit_error_reuses_the_bound_request(tmp_path):
    item = _proposed(tmp_path)
    calls = 0

    def leaf(vault, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("temporary writer failure")
        path = vault / "Knowledge Base/Entities/Organizations/acme-labs.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "---\ntype: entity\nentity_type: organization\ntitle: Acme Labs\n---\nA durable organization identity.\n"
        )
        writer_lease.mark_active_mutation_committed()
        return {"path": str(path.relative_to(vault))}

    command = SimpleNamespace(name="connect_memory", leaf=leaf, read_only=False)
    manager = writer_lease.LeaseManager(writer_lease.LeaseConfig(state_dir=tmp_path / "lease"))
    kwargs = {
        "operation": "create-entity",
        "entity_type": "organization",
        "name": "Acme Labs",
        "summary": "A durable organization identity.",
        "vocabulary_ref": item.ref,
        "vocabulary_fingerprint": item.fingerprint,
    }
    with library_scope(), pytest.raises(OSError, match="temporary writer failure"):
        manager.invoke(command, (tmp_path,), kwargs, idempotency_key="retryable-reviewed-write")
    with library_scope():
        completed = manager.invoke(command, (tmp_path,), kwargs, idempotency_key="retryable-reviewed-write")
        replay = manager.invoke(command, (tmp_path,), kwargs, idempotency_key="retryable-reviewed-write")
    assert calls == 2
    assert replay == completed
    assert VocabularyState(tmp_path).get(item.ref)["state"] == "applied"


def test_committed_terminal_recovers_an_uncertain_direct_application(tmp_path, monkeypatch):
    item = _proposed(tmp_path)
    path = "Knowledge Base/Entities/Organizations/acme-labs.md"

    def leaf(vault, **kwargs):
        target = vault / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            "---\ntype: entity\nentity_type: organization\ntitle: Acme Labs\n---\nA durable organization identity.\n"
        )
        writer_lease.mark_active_mutation_committed()
        return {"path": path}

    command = SimpleNamespace(name="connect_memory", leaf=leaf, read_only=False)
    manager = writer_lease.LeaseManager(writer_lease.LeaseConfig(state_dir=tmp_path / "lease"))
    kwargs = {
        "operation": "create-entity",
        "entity_type": "organization",
        "name": "Acme Labs",
        "summary": "A durable organization identity.",
        "vocabulary_ref": item.ref,
        "vocabulary_fingerprint": item.fingerprint,
    }
    real_commit = vocabulary_application.commit
    monkeypatch.setattr(
        vocabulary_application,
        "commit",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("state write unavailable")),
    )
    with library_scope():
        first = manager.invoke(command, (tmp_path,), kwargs, idempotency_key="uncertain-direct")
    assert first["state"] == "committed"
    assert VocabularyState(tmp_path).get(item.ref)["decision"]["application"]["state"] == "uncertain"
    monkeypatch.setattr(vocabulary_application, "commit", real_commit)
    with library_scope():
        replay = manager.invoke(command, (tmp_path,), kwargs, idempotency_key="uncertain-direct")
    assert replay == first
    assert VocabularyState(tmp_path).get(item.ref)["state"] == "applied"


def test_curation_ledger_stays_applying_until_the_sealed_plan_completes(tmp_path):
    item = _proposed(tmp_path)
    store = VocabularyState(tmp_path)
    choice = _decision(item)["choice"]
    binding = {
        "run_id": "cur-20260907-" + "a" * 12,
        "plan_id": "a" * 64,
        "plan_fingerprint": "b" * 64,
    }
    store.begin_curation_application(
        item, request_id="request-1", operation_id="operation-1", choice=choice, **binding
    )
    partial = store.record_curation_receipts(
        item,
        operation_id="operation-1",
        phase="partial",
        receipts=[{"operation_id": "curation-step-1"}],
    )
    assert partial["state"] == "applying"
    store.begin_curation_application(
        item, request_id="request-2", operation_id="operation-2", choice=choice, **binding
    )
    completed = store.record_curation_receipts(
        item,
        operation_id="operation-2",
        phase="completed",
        receipts=[
            {"operation_id": "curation-step-1"},
            {"operation_id": "curation-step-2"},
        ],
    )
    assert completed["state"] == "applied"
    assert completed["receipts"] == ["curation-step-1", "curation-step-2"]


def test_activated_operation_mints_repeatable_ids_without_changing_v1_ids(tmp_path, monkeypatch):
    class Authority:
        def __init__(self, _root):
            pass

        def runtime_status(self):
            return SimpleNamespace(mode="v2")

        @staticmethod
        def _now(_clock):
            return 0

        @staticmethod
        def _custody_loader(_root, *, now):
            del now
            return SimpleNamespace(control=SimpleNamespace(logical_vault_id="vault-logical-id"))

    monkeypatch.setattr(vocabulary_gate.vocabulary_authority, "VocabularyAuthority", Authority)
    principal = SimpleNamespace(audience_id="owner")

    def generated(key):
        with vocabulary_gate.operation_context(
            tmp_path,
            idempotency_key=key,
            command_digest="a" * 64,
            receipt_id="receipt",
            principal=principal,
        ):
            return memory_refs.new_id(), memory_refs.new_id()

    first = generated("same-operation")
    assert first == generated("same-operation")
    assert first != generated("different-operation")
    assert uuid.UUID(first[0]).version == 5
    assert memory_refs.new_id() != memory_refs.new_id()


def test_denied_entity_identity_is_exactly_approved_and_retried_with_the_same_key(
    tmp_path, monkeypatch
):
    from test_vocabulary_authority import (
        _activate,
        _approve_request,
        _principal,
        _store,
        install_unit_session_boundary,
    )

    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "state"))
    install_unit_session_boundary(monkeypatch)
    authority = _store(tmp_path / "vault")
    principal = _principal()
    _activate(authority, principal)
    # The gate uses the real authority fixture and its reserve/request paths;
    # only construction is redirected so this unit has the same custody setup.
    monkeypatch.setattr(vocabulary_gate.vocabulary_authority, "VocabularyAuthority", lambda _root: authority)

    def staged_identity():
        with vocabulary_gate.operation_context(
            tmp_path / "vault",
            idempotency_key="approved-retry",
            command_digest="a" * 64,
            receipt_id="receipt",
            principal=principal,
        ):
            return memory_refs.new_id()

    first = staged_identity()
    from exomem.vocabulary_effects import CanonicalWriteImage, _result

    images = (CanonicalWriteImage("Knowledge Base/Entities/Organizations/acme.md", None, b"# Example\n"),)
    effects = (Effect("entity.create", images[0].path, f"memory:{first}"),)
    operation = vocabulary_authority.CanonicalOperation.from_effects(
        operation_id="entity-create-" + first,
        command_digest="a" * 64,
        effects=effects,
        image_digest=_result("reviewed", effects, (), images).digest,
        registry_digests={"entity_types": "c" * 64},
        target_digests={f"memory:{first}": "d" * 64},
    )
    with pytest.raises(vocabulary_authority.VocabularyAuthorityDenied):
        authority.reserve(operation, principal=principal)
    assert not (tmp_path / "vault" / "Knowledge Base").exists()

    request = authority.request(operation, principal=principal, expires_at=1_700_000_100, images=images)
    _approve_request(authority, principal, request.request_id)
    retried = staged_identity()
    assert retried == first
    assert authority.reserve(operation, principal=principal).authority_ids
    assert staged_identity() != memory_refs.new_id()
