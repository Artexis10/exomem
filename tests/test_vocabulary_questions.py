from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from exomem import commands, epistemic_graph, vocabulary_review
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
        f"---\ntype: note\nexomem_id: {identity}\n---\n{body}\n",
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
