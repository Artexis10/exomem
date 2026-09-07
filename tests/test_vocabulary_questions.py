from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from exomem import (
    commands,
    epistemic_graph,
    mutation_terminal,
    relation_registry,
    semantic_contract,
    vocabulary_application,
    vocabulary_questions,
    vocabulary_review,
    writer_lease,
)
from exomem.governance.principal import library_scope
from exomem.vocabulary_state import VocabularyState
from exomem.vocabulary_workflow import Evidence, make_item

_ANCHOR = "Knowledge Base/Notes/meaning-question.md"
_IDENTITY = "00000000-0000-4000-8000-000000000001"


def _anchor(
    vault: Path,
    body: str = "A durable fact.",
    *,
    path: str = _ANCHOR,
    identity: str = _IDENTITY,
) -> str:
    page = vault / path
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(
        f"---\ntype: insight\nstatus: active\nexomem_id: {identity}\n---\n{body}\n",
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize(
    "family",
    ["entity-instance/v1", "entity-type/v1", "relation-type/v1"],
)
def test_explicit_meaning_question_uses_path_when_graph_is_unavailable(
    tmp_path: Path, family: str
) -> None:
    path = _anchor(tmp_path)

    result = commands.op_review_memory(
        tmp_path,
        mode="vocabulary",
        path=path,
        query="Does this need a more specific meaning?",
        family=family,
    )

    item = result["item"]
    assert item["family"] == family
    assert item["signal"] == "agent-meaning-question"
    assert item["question"] == "Does this need a more specific meaning?"
    assert item["target_versions"] == {
        path: hashlib.sha256((tmp_path / path).read_bytes()).hexdigest()
    }
    assert result["context_route"] == {"tool": "review_item_context", "ref": item["ref"]}
    assert commands.op_review_item_context(tmp_path, ref=item["ref"])["item"]["question"] == (
        "Does this need a more specific meaning?"
    )


def test_duplicate_anchor_ids_remain_distinct_path_bound_questions(tmp_path: Path) -> None:
    first_path = _anchor(tmp_path)
    second_path = _anchor(
        tmp_path,
        "A distinct page with the same malformed identity ownership.",
        path="Knowledge Base/Notes/duplicate-meaning-question.md",
    )
    epistemic_graph.EpistemicGraphIndex(tmp_path).rebuild_all()

    first = commands.op_review_memory(
        tmp_path,
        mode="vocabulary",
        path=first_path,
        query="Does this distinction matter?",
        family="relation-type/v1",
    )
    second = commands.op_review_memory(
        tmp_path,
        mode="vocabulary",
        path=second_path,
        query="Does this distinction matter?",
        family="relation-type/v1",
    )

    assert set(first["item"]["target_versions"]) == {first_path}
    assert set(second["item"]["target_versions"]) == {second_path}
    assert first["item"]["ref"] != second["item"]["ref"]


def test_unique_current_graph_anchor_uses_stable_memory_ref(tmp_path: Path) -> None:
    path = _anchor(tmp_path)
    epistemic_graph.EpistemicGraphIndex(tmp_path).rebuild_all()

    result = commands.op_review_memory(
        tmp_path,
        mode="vocabulary",
        path=path,
        query="Does this need a more specific meaning?",
        family="relation-type/v1",
    )

    assert set(result["item"]["target_versions"]) == {
        "exomem://memory/00000000-0000-4000-8000-000000000001"
    }


def test_questions_deduplicate_only_when_anchor_and_question_match(tmp_path: Path) -> None:
    path = _anchor(tmp_path)
    first = commands.op_review_memory(
        tmp_path,
        mode="vocabulary",
        path=path,
        query="Does this distinction matter?",
        family="relation-type/v1",
    )
    same = commands.op_review_memory(
        tmp_path,
        mode="vocabulary",
        path=path,
        query="Does this distinction matter?",
        family="relation-type/v1",
    )
    changed_question = commands.op_review_memory(
        tmp_path,
        mode="vocabulary",
        path=path,
        query="Is the generic connection enough?",
        family="relation-type/v1",
    )
    _anchor(tmp_path, "The durable fact changed.")
    changed_content = commands.op_review_memory(
        tmp_path,
        mode="vocabulary",
        path=path,
        query="Does this distinction matter?",
        family="relation-type/v1",
    )

    assert same["item"]["ref"] == first["item"]["ref"]
    assert same["item"]["fingerprint"] == first["item"]["fingerprint"]
    assert changed_question["item"]["ref"] != first["item"]["ref"]
    assert changed_content["item"]["ref"] == first["item"]["ref"]
    assert changed_content["item"]["fingerprint"] != first["item"]["fingerprint"]


@pytest.mark.parametrize(
    "origins",
    [
        pytest.param(["markdown_relation", "semantic_relation"], id="both-origins"),
        pytest.param(["semantic_relation"], id="default-semantic-origin"),
    ],
)
def test_relation_question_binds_the_exact_current_queue_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, origins: list[str]
) -> None:
    source = _anchor(
        tmp_path,
        "See [[Knowledge Base/Notes/meaning-target]].",
        path="Knowledge Base/Notes/meaning-source.md",
    )
    target = _anchor(
        tmp_path,
        "A distinct target.",
        path="Knowledge Base/Notes/meaning-target.md",
        identity="00000000-0000-4000-8000-000000000002",
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
                        "origins": origins,
                    }
                },
            },
        )
        epistemic_graph.EpistemicGraphIndex(tmp_path).rebuild_all()
    monkeypatch.delenv("EXOMEM_DISABLE_CORPUS_CACHE", raising=False)
    semantic_contract.build_corpus_context(tmp_path)
    queue = commands.op_review_memory(tmp_path, mode="relation-queue")
    candidate = next(
        entry
        for group in queue["groups"]
        for entry in group["items"]
        if entry["from"] == source and entry["to"] == target
    )

    result = commands.op_review_memory(
        tmp_path,
        mode="vocabulary",
        path=source,
        query="Should this relation remain generic?",
        family="relation-type/v1",
        ref=candidate["ref"],
    )

    item = result["item"]
    assert set(item["target_versions"]) == {
        "exomem://memory/00000000-0000-4000-8000-000000000001",
        "exomem://memory/00000000-0000-4000-8000-000000000002",
    }
    assert {entry["origin"] for entry in item["evidence"]} == {"agent-meaning-question"}
    assert result["application_route"] == {
        "tool": "connect_memory",
        "operation": "accept-relation",
        "ref": candidate["ref"],
        "path": source,
        "expected_fingerprint": candidate["fingerprint"],
        "expected_hash": candidate["source_content_hash"],
        "requested_relation": None,
        "instructions": (
            "Record a reuse or propose-new decision for this paired item, then set "
            "requested_relation before invoking this route."
        ),
        "vocabulary_ref": item["ref"],
        "vocabulary_fingerprint": item["fingerprint"],
    }
    assert item["projection"]["currency"] == {
        "candidate_ref": candidate["ref"],
        "candidate_fingerprint": candidate["fingerprint"],
        "candidate_source_path": source,
        "candidate_target_path": target,
    }
    decision = {
        "item_ref": item["ref"],
        "fingerprint": item["fingerprint"],
        "family": item["family"],
        "registry_hashes": item["registry_hashes"],
        "target_versions": item["target_versions"],
        "outcome": "reuse",
        "choice": {"canonical": "venue.hosts"},
        "rationale": "The reviewed directed candidate keeps the existing link meaning.",
    }
    with library_scope():
        vocabulary_review.decide(tmp_path, ref=item["ref"], decision=decision)
    route = result["application_route"]
    arguments = {
        key: route[key]
        for key in (
            "operation",
            "ref",
            "path",
            "expected_fingerprint",
            "expected_hash",
            "vocabulary_ref",
            "vocabulary_fingerprint",
        )
    } | {
        "requested_relation": "venue.hosts",
        "why": "Apply the reviewed directed link.",
    }
    command = next(entry for entry in commands.PRODUCT_COMMANDS if entry.name == "connect_memory")
    manager = writer_lease.LeaseManager(writer_lease.LeaseConfig(state_dir=tmp_path / "lease"))
    with library_scope():
        accepted = manager.invoke(
            command,
            (tmp_path,),
            arguments,
            idempotency_key="question-directed-link",
            read_only=False,
        )
    assert accepted["state"] == "committed"
    assert accepted["receipt_id"]
    assert "- venue.hosts [[Knowledge Base/Notes/meaning-target]]" in (
        tmp_path / source
    ).read_text(encoding="utf-8")
    context = commands.op_review_item_context(tmp_path, ref=item["ref"])
    assert context["item"]["state"] == "applied"
    assert context["item"]["decision_currency"] == "current"
    assert context["item"]["receipts"] == [accepted["receipt_id"]]
    state_path = VocabularyState(tmp_path).store.path
    before_restart = state_path.read_bytes()
    restarted_context = commands.op_review_item_context(tmp_path, ref=item["ref"])
    assert restarted_context["item"]["state"] == "applied"
    assert restarted_context["item"]["decision_currency"] == "current"
    assert restarted_context["item"]["receipts"] == [accepted["receipt_id"]]
    assert state_path.read_bytes() == before_restart
    with library_scope():
        epistemic_graph.EpistemicGraphIndex(tmp_path).rebuild_all()
    graph = commands.op_graph_context(tmp_path, path=source, relation_types=["venue.hosts"])
    assert graph["edges"]
    assert graph["edges"][0]["relation_type"] == "venue.hosts"


def test_relation_question_refuses_wrong_or_withheld_endpoint_without_state_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _anchor(
        tmp_path,
        "See [[Knowledge Base/Notes/meaning-target]].",
        path="Knowledge Base/Notes/meaning-source.md",
    )
    target = _anchor(
        tmp_path,
        "A distinct target.",
        path="Knowledge Base/Notes/meaning-target.md",
        identity="00000000-0000-4000-8000-000000000002",
    )
    with library_scope():
        epistemic_graph.EpistemicGraphIndex(tmp_path).rebuild_all()
    monkeypatch.delenv("EXOMEM_DISABLE_CORPUS_CACHE", raising=False)
    semantic_contract.build_corpus_context(tmp_path)
    queue = commands.op_review_memory(tmp_path, mode="relation-queue")
    candidate = next(
        entry
        for group in queue["groups"]
        for entry in group["items"]
        if entry["from"] == source and entry["to"] == target
    )
    state_path = VocabularyState(tmp_path).store.path

    with pytest.raises(ValueError, match="REVIEW_REFRESH_REQUIRED"):
        vocabulary_questions.submit(
            tmp_path,
            path=target,
            query="Should this reverse relation be reviewed?",
            family="relation-type/v1",
            relation_ref=candidate["ref"],
        )
    assert not state_path.exists()

    original_release = vocabulary_review.egress.release_level_for
    monkeypatch.setattr(
        vocabulary_review.egress,
        "release_level_for",
        lambda vault, path: 0 if path == target else original_release(vault, path),
    )
    with pytest.raises(ValueError, match="VOCABULARY_ITEM_NOT_FOUND"):
        vocabulary_questions.submit(
            tmp_path,
            path=source,
            query="Should this relation remain generic?",
            family="relation-type/v1",
            relation_ref=candidate["ref"],
        )
    assert not state_path.exists()
    monkeypatch.undo()

    with library_scope():
        commands.op_triage_memory(
            tmp_path,
            ref=candidate["ref"],
            action="dismiss",
            why="handled: the relation queue candidate was reviewed already.",
            expected_fingerprint=candidate["fingerprint"],
            source_path=source,
        )
    with pytest.raises(ValueError, match="relation candidate is no longer eligible"):
        vocabulary_questions.submit(
            tmp_path,
            path=source,
            query="Should this relation remain generic?",
            family="relation-type/v1",
            relation_ref=candidate["ref"],
        )
    assert b"exomem://review/vocabulary/" not in state_path.read_bytes()


def test_relation_question_refuses_an_endpoint_changed_after_its_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _anchor(
        tmp_path,
        "See [[Knowledge Base/Notes/meaning-target]].",
        path="Knowledge Base/Notes/meaning-source.md",
    )
    target = _anchor(
        tmp_path,
        "A distinct target.",
        path="Knowledge Base/Notes/meaning-target.md",
        identity="00000000-0000-4000-8000-000000000002",
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
    monkeypatch.delenv("EXOMEM_DISABLE_CORPUS_CACHE", raising=False)
    semantic_contract.build_corpus_context(tmp_path)
    queue = commands.op_review_memory(tmp_path, mode="relation-queue")
    candidate = next(
        entry
        for group in queue["groups"]
        for entry in group["items"]
        if entry["from"] == source and entry["to"] == target
    )
    result = vocabulary_questions.submit(
        tmp_path,
        path=source,
        query="Should this relation remain generic?",
        family="relation-type/v1",
        relation_ref=candidate["ref"],
    )
    item = result["item"]
    with library_scope():
        vocabulary_review.decide(
            tmp_path,
            ref=item["ref"],
            decision={
                "item_ref": item["ref"],
                "fingerprint": item["fingerprint"],
                "family": item["family"],
                "registry_hashes": item["registry_hashes"],
                "target_versions": item["target_versions"],
                "outcome": "reuse",
                "choice": {"canonical": "venue.hosts"},
                "rationale": "The reviewed directed candidate keeps this meaning.",
            },
        )
    _anchor(
        tmp_path,
        "The target changed after the reviewed decision.",
        path=target,
        identity="00000000-0000-4000-8000-000000000002",
    )
    route = result["application_route"]
    arguments = {
        key: route[key]
        for key in (
            "operation",
            "ref",
            "path",
            "expected_fingerprint",
            "expected_hash",
            "vocabulary_ref",
            "vocabulary_fingerprint",
        )
    } | {
        "requested_relation": "venue.hosts",
        "why": "Apply the reviewed directed link.",
    }
    command = next(entry for entry in commands.PRODUCT_COMMANDS if entry.name == "connect_memory")
    manager = writer_lease.LeaseManager(writer_lease.LeaseConfig(state_dir=tmp_path / "lease"))
    with library_scope(), pytest.raises(ValueError, match="VOCABULARY_DECISION_STALE"):
        manager.invoke(
            command,
            (tmp_path,),
            arguments,
            idempotency_key="changed-question-directed-link",
            read_only=False,
        )
    assert "## Relations" not in (tmp_path / source).read_text(encoding="utf-8")


def test_relation_question_rechecks_dismissal_while_observing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _anchor(
        tmp_path,
        "See [[Knowledge Base/Notes/meaning-target]].",
        path="Knowledge Base/Notes/meaning-source.md",
    )
    target = _anchor(
        tmp_path,
        "A distinct target.",
        path="Knowledge Base/Notes/meaning-target.md",
        identity="00000000-0000-4000-8000-000000000002",
    )
    with library_scope():
        epistemic_graph.EpistemicGraphIndex(tmp_path).rebuild_all()
    monkeypatch.delenv("EXOMEM_DISABLE_CORPUS_CACHE", raising=False)
    semantic_contract.build_corpus_context(tmp_path)
    queue = commands.op_review_memory(tmp_path, mode="relation-queue")
    candidate = next(
        entry
        for group in queue["groups"]
        for entry in group["items"]
        if entry["from"] == source and entry["to"] == target
    )
    original_observe = vocabulary_questions.VocabularyState.observe

    def dismiss_then_observe(self, item, **kwargs):
        with library_scope():
            commands.op_triage_memory(
                tmp_path,
                ref=candidate["ref"],
                action="dismiss",
                why="handled: the candidate was dismissed before observation.",
                expected_fingerprint=candidate["fingerprint"],
                source_path=source,
            )
        return original_observe(self, item, **kwargs)

    monkeypatch.setattr(
        vocabulary_questions.VocabularyState, "observe", dismiss_then_observe
    )
    with pytest.raises(ValueError, match="relation candidate is no longer eligible"):
        commands.op_review_memory(
            tmp_path,
            mode="vocabulary",
            path=source,
            query="Should this relation remain generic?",
            family="relation-type/v1",
            ref=candidate["ref"],
        )
    state_path = VocabularyState(tmp_path).store.path
    assert b"exomem://review/vocabulary/" not in state_path.read_bytes()


def test_directed_relation_questions_keep_an_applying_forward_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _anchor(
        tmp_path,
        "See [[Knowledge Base/Notes/meaning-target]].",
        path="Knowledge Base/Notes/meaning-source.md",
    )
    target = _anchor(
        tmp_path,
        "See [[Knowledge Base/Notes/meaning-source]].",
        path="Knowledge Base/Notes/meaning-target.md",
        identity="00000000-0000-4000-8000-000000000002",
    )
    extension = {
        "parent": "relates_to",
        "description": "A venue hosts a recurring event.",
        "direction": "directed",
        "origins": ["markdown_relation", "semantic_relation"],
    }
    with library_scope():
        relation_registry.save_registry(
            tmp_path,
            {"schema_version": 1, "extensions": {"venue.hosts": extension}},
        )
        epistemic_graph.EpistemicGraphIndex(tmp_path).rebuild_all()
    monkeypatch.delenv("EXOMEM_DISABLE_CORPUS_CACHE", raising=False)
    semantic_contract.build_corpus_context(tmp_path)
    queue = commands.op_review_memory(tmp_path, mode="relation-queue")
    candidates = {
        (entry["from"], entry["to"]): entry
        for group in queue["groups"]
        for entry in group["items"]
    }
    forward_candidate = candidates[source, target]
    reverse_candidate = candidates[target, source]
    question = "Should this relation remain generic?"
    forward = commands.op_review_memory(
        tmp_path,
        mode="vocabulary",
        path=source,
        query=question,
        family="relation-type/v1",
        ref=forward_candidate["ref"],
    )
    item = forward["item"]
    with library_scope():
        vocabulary_review.decide(
            tmp_path,
            ref=item["ref"],
            decision={
                "item_ref": item["ref"],
                "fingerprint": item["fingerprint"],
                "family": item["family"],
                "registry_hashes": item["registry_hashes"],
                "target_versions": item["target_versions"],
                "outcome": "reuse",
                "choice": {"canonical": "venue.hosts"},
                "rationale": "The reviewed directed candidate uses this relation.",
            },
        )
    route = forward["application_route"]
    binding_kwargs = {
        key: route[key]
        for key in (
            "operation",
            "ref",
            "path",
            "expected_fingerprint",
            "expected_hash",
            "vocabulary_ref",
            "vocabulary_fingerprint",
        )
    } | {"requested_relation": "venue.hosts", "why": "Apply the reviewed edge."}
    with library_scope():
        binding = vocabulary_application.bind(
            tmp_path,
            command="connect_memory",
            kwargs=binding_kwargs,
            idempotency_key="forward-paired-question",
            command_digest="a" * 64,
            principal="owner",
        )
    assert VocabularyState(tmp_path).get(item["ref"])["state"] == "applying"

    reverse = commands.op_review_memory(
        tmp_path,
        mode="vocabulary",
        path=target,
        query=question,
        family="relation-type/v1",
        ref=reverse_candidate["ref"],
    )
    assert reverse["item"]["ref"] != item["ref"]
    assert VocabularyState(tmp_path).get(item["ref"])["state"] == "applying"

    source_page = tmp_path / source
    source_page.write_text(
        source_page.read_text(encoding="utf-8")
        + "\n## Relations\n\n- venue.hosts [[Knowledge Base/Notes/meaning-target]]\n",
        encoding="utf-8",
    )
    with library_scope():
        applied = vocabulary_application.commit(
            tmp_path,
            binding,
            vocabulary_application._writer_terminal(
                {
                    "_terminal": mutation_terminal._TERMINAL_MARKER,
                    "version": mutation_terminal._TERMINAL_VERSION,
                    "state": "committed",
                    "ok": True,
                    "receipt_id": "forward-paired-question-receipt",
                    "leaf_result": {
                        "from": source,
                        "to": target,
                        "relation_type": "venue.hosts",
                    },
                }
            ),
        )
    assert applied["state"] == "applied"
    assert VocabularyState(tmp_path).get(item["ref"])["receipts"] == [
        "forward-paired-question-receipt"
    ]


def test_question_submission_refuses_unknown_family_and_unreadable_anchor_without_state_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _anchor(tmp_path)
    store = VocabularyState(tmp_path).store.path
    with pytest.raises(ValueError, match="VOCABULARY_FAMILY_UNSUPPORTED"):
        commands.op_review_memory(
            tmp_path,
            mode="vocabulary",
            path=path,
            query="Does this distinction matter?",
            family="unsupported/v1",
        )
    assert not store.exists()

    with pytest.raises(ValueError, match="VOCABULARY_EVIDENCE_UNAVAILABLE"):
        commands.op_review_memory(
            tmp_path,
            mode="vocabulary",
            path="Knowledge Base/Notes/missing.md",
            query="Does this distinction matter?",
            family="relation-type/v1",
        )
    assert not store.exists()

    monkeypatch.setattr(vocabulary_review.egress, "release_level_for", lambda *args: 0)
    with pytest.raises(ValueError, match="VOCABULARY_ITEM_NOT_FOUND"):
        commands.op_review_memory(
            tmp_path,
            mode="vocabulary",
            path=path,
            query="Does this distinction matter?",
            family="relation-type/v1",
        )
    assert not store.exists()


def test_question_variant_rejects_partial_or_paginated_vocabulary_arguments(tmp_path: Path) -> None:
    path = _anchor(tmp_path)
    with pytest.raises(ValueError, match="INVALID_VOCABULARY_REVIEW_ARGUMENTS"):
        commands.op_review_memory(tmp_path, mode="vocabulary", path=path)
    with pytest.raises(ValueError, match="INVALID_VOCABULARY_REVIEW_ARGUMENTS"):
        commands.op_review_memory(
            tmp_path,
            mode="vocabulary",
            path=path,
            query="Does this distinction matter?",
            family="relation-type/v1",
            continuation="cursor",
        )
    with pytest.raises(ValueError, match="INVALID_VOCABULARY_REVIEW_ARGUMENTS"):
        commands.op_review_memory(
            tmp_path,
            mode="vocabulary",
            path=path,
            query="Does this distinction matter?",
            family="relation-type/v1",
            ref="exomem://review/vocabulary/other",
        )
    with pytest.raises(ValueError, match="INVALID_REVIEW_ARGUMENTS"):
        commands.op_review_memory(tmp_path, mode="attention", family="relation-type/v1")


def test_existing_questionless_work_item_serialization_is_unchanged() -> None:
    item = make_item(
        family="relation-type/v1",
        signal="generic-pair",
        targets={"anchor": "content"},
        evidence=[Evidence("anchor", "content", "origin")],
        registry_hashes={"relations": "registry"},
        projection_status="current",
    )

    assert "question" not in item.to_dict()


def test_question_variant_reaches_same_leaf_over_mcp_rest_and_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from conftest import initialize_vault_state_offline

    from exomem import server as server_module
    from exomem.__main__ import main
    from exomem.init import init_vault

    vault = tmp_path / "vault"
    init_vault(vault)
    initialize_vault_state_offline(vault, source="vocabulary questions")
    path = _anchor(vault)
    request = {
        "mode": "vocabulary",
        "path": path,
        "query": "Does this distinction matter?",
        "family": "relation-type/v1",
    }
    monkeypatch.setattr(server_module, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_RELEVANCE_CHECK", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_CLIP", "1")
    monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", "python")
    monkeypatch.setenv("EXOMEM_DISABLE_FILE_WATCHER", "1")
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-lease"))
    monkeypatch.setenv("EXOMEM_REST_API_KEY", "test-key")

    direct = commands.op_review_memory(vault, **request)
    mcp = server_module.build_server(require_auth=False)
    mcp_result = asyncio.run(mcp.call_tool("review_memory", request, run_middleware=True))
    rest = TestClient(mcp.http_app()).post(
        "/api/review_memory",
        json=request,
        headers={"Authorization": "Bearer test-key"},
    )

    assert mcp_result.structured_content == direct
    assert rest.status_code == 200, rest.text
    assert rest.json() == {"success": True, "data": direct}
    assert (
        main(
            [
                "review_memory",
                "--mode",
                "vocabulary",
                "--path",
                path,
                "--query",
                request["query"],
                "--family",
                request["family"],
                "--json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == {"success": True, "data": direct}
