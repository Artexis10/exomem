"""Bounded advisory category feedback derived once in the shared write leaf.

Covers OpenSpec `teach-portable-category-core` tasks 1.3 (feedback half) and
3.3: the shared semantic write result exposes at most eight deterministic
`category_feedback` entries plus `category_feedback_omitted`, using exact
registry resolution with page project/page-type context, and every adapter
projection (MCP/REST/CLI) inherits byte-equivalent data.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from exomem import (
    memory_schema,
    relation_registry,
    relation_review,
    semantic_contract,
    semantic_language_registry,
    semantic_writes,
    server,
)
from exomem.__main__ import main

REPO_ROOT = Path(__file__).resolve().parents[1]

_ID = "00000000-0000-0000-0000-000000000010"


def _empty_contracts() -> memory_schema.ResolvedMemoryContracts:
    return memory_schema.ResolvedMemoryContracts(
        validation="off",
        matched_contracts=(),
        constraints=(),
        conflicts=(),
    )


def _source(body: str, *, project: str | None = "atlas") -> str:
    fields = ["title: Page", "status: active", f"exomem_id: {_ID}"]
    if project is not None:
        fields.append(f"project: {project}")
    return "---\n" + "\n".join(fields) + "\n---\n\n" + body


def _registry(
    categories: dict[str, Any] | None = None,
) -> semantic_language_registry.SemanticLanguageRegistry:
    if categories is None:
        return semantic_language_registry.core_registry()
    return semantic_language_registry.load_registry(
        proposal={
            "schema_version": semantic_language_registry.SCHEMA_VERSION,
            "categories": categories,
            "kinds": {},
        }
    )


def _state(
    tmp_path: Path,
    body: str,
    *,
    language: semantic_language_registry.SemanticLanguageRegistry,
    project: str | None = "atlas",
    rel_path: str = "Knowledge Base/Notes/note.md",
) -> semantic_contract.SemanticPageState:
    return semantic_contract.build_page_state(
        tmp_path,
        rel_path,
        _source(body, project=project),
        relation_registry=relation_registry.core_registry(),
        language_registry=language,
        review_fingerprint="review-v1",
    )


def _feedback(
    tmp_path: Path,
    body: str,
    *,
    language: semantic_language_registry.SemanticLanguageRegistry | None = None,
    project: str | None = "atlas",
) -> semantic_contract.SemanticContractResult:
    resolved = language or _registry()
    state = _state(tmp_path, body, language=resolved, project=project)
    corpus = semantic_contract.SemanticCorpusContext.from_states(
        tmp_path,
        (state,),
        registry=relation_registry.core_registry(),
        identity_census=semantic_contract.StableIdentityCensus(
            (semantic_contract.StableIdentityEntry(state.path, state.identity),)
        ),
    )
    empty = semantic_contract.SemanticCorpusContext.from_states(
        tmp_path,
        (),
        registry=relation_registry.core_registry(),
        identity_census=semantic_contract.StableIdentityCensus(()),
    )
    return semantic_contract.evaluate(
        before=None,
        after=state,
        operation="create",
        mode="precommit",
        before_contracts=_empty_contracts(),
        after_contracts=_empty_contracts(),
        before_corpus=empty,
        after_corpus=corpus,
        include_relation_disposition=False,
        language_registry=resolved,
    )


def _entries(result: semantic_contract.SemanticContractResult) -> list[dict[str, Any]]:
    return [entry.as_dict() for entry in result.category_feedback]


# --- Built-in alias, unknown, and ordinary-core behavior -----------------


def test_builtin_alias_reports_canonical_reuse(tmp_path: Path) -> None:
    result = _feedback(tmp_path, "- [constraints] Keep retry windows bounded #code\n")

    assert _entries(result) == [
        {
            "unit_ref": result.category_feedback[0].unit_ref,
            "authored": "constraints",
            "normalized": "constraints",
            "canonical": "constraint",
            "status": "alias",
            "replacement": None,
        }
    ]
    assert result.category_feedback[0].unit_ref is not None
    assert result.category_feedback_omitted == 0


def test_unknown_wellformed_category_is_open(tmp_path: Path) -> None:
    result = _feedback(tmp_path, "- [telemetry] Watch the p99 latency.\n")

    (entry,) = _entries(result)
    assert entry["status"] == "open"
    assert entry["normalized"] == "telemetry"
    assert entry["canonical"] == "telemetry"
    assert entry["replacement"] is None


def test_ordinary_core_and_canonical_categories_produce_no_entry(
    tmp_path: Path,
) -> None:
    language = _registry({"widget": {"description": "A vault-owned widget."}})
    result = _feedback(
        tmp_path,
        "- [constraint] Canonical core role.\n- [widget] Canonical extension role.\n",
        language=language,
    )

    assert result.category_feedback == ()
    assert result.category_feedback_omitted == 0


def test_rich_units_cover_explicit_category_but_not_defaulted_kind(
    tmp_path: Path,
) -> None:
    body = (
        "## Decision\n\n"
        "A rich decision whose category defaults to its kind.\n\n"
        "## Finding\n"
        "- category: constraints\n\n"
        "A rich finding authored with a built-in alias category.\n"
    )
    result = _feedback(tmp_path, body)

    # The defaulted `## Decision` category equals its core kind -> no entry.
    assert [entry["authored"] for entry in _entries(result)] == ["constraints"]
    (entry,) = _entries(result)
    assert entry["status"] == "alias"
    assert entry["canonical"] == "constraint"


# --- Deprecated / scope-violation extension categories --------------------


def test_deprecated_extension_reports_replacement(tmp_path: Path) -> None:
    language = _registry(
        {
            "legacy_note": {
                "description": "The retired note category.",
                "status": "deprecated",
                "replaced_by": "modern_note",
            },
            "modern_note": {"description": "The active note category."},
        }
    )
    result = _feedback(tmp_path, "- [legacy_note] Superseded label.\n", language=language)

    (entry,) = _entries(result)
    assert entry["status"] == "deprecated"
    assert entry["canonical"] == "legacy_note"
    assert entry["replacement"] == "modern_note"


def test_scope_violation_is_reported_with_nullable_replacement(
    tmp_path: Path,
) -> None:
    language = _registry(
        {"scoped_cat": {"description": "Scoped elsewhere.", "scope": {"projects": ["other"]}}}
    )
    result = _feedback(tmp_path, "- [scoped_cat] Outside its project scope.\n", language=language)

    (entry,) = _entries(result)
    assert entry["status"] == "scope_violation"
    assert entry["canonical"] == "scoped_cat"
    assert entry["replacement"] is None


# --- Bounded, deterministic truncation ------------------------------------


def test_feedback_is_truncated_to_eight_with_omitted_count(tmp_path: Path) -> None:
    aliases = (
        "decisions",
        "facts",
        "findings",
        "insights",
        "constraints",
        "requirements",
        "assumptions",
        "risks",
        "problems",  # ninth qualifying alias entry
    )
    body = "".join(f"- [{alias}] Observation {index}.\n" for index, alias in enumerate(aliases))
    result = _feedback(tmp_path, body)

    assert len(result.category_feedback) == 8
    assert result.category_feedback_omitted == 1
    # Deterministic source order: the first eight aliases are retained.
    assert [entry.authored for entry in result.category_feedback] == list(aliases[:8])


# --- Derived once; adapters inherit byte-equivalent data -------------------


def test_evaluate_without_registry_yields_no_feedback(tmp_path: Path) -> None:
    state = _state(tmp_path, "- [constraints] Bounded.\n", language=_registry())
    corpus = semantic_contract.SemanticCorpusContext.from_states(
        tmp_path,
        (state,),
        registry=relation_registry.core_registry(),
        identity_census=semantic_contract.StableIdentityCensus(
            (semantic_contract.StableIdentityEntry(state.path, state.identity),)
        ),
    )
    result = semantic_contract.evaluate(
        before=None,
        after=state,
        operation="create",
        mode="precommit",
        before_contracts=_empty_contracts(),
        after_contracts=_empty_contracts(),
        before_corpus=corpus,
        after_corpus=corpus,
        include_relation_disposition=False,
    )

    assert result.category_feedback == ()
    assert result.category_feedback_omitted == 0


def test_result_dict_and_adapter_projection_are_byte_equivalent(tmp_path: Path) -> None:
    result = _feedback(tmp_path, "- [constraints] Keep retry windows bounded.\n")

    canonical = [entry.as_dict() for entry in result.category_feedback]
    result_dict = result.as_dict()
    adapter_dict = semantic_writes._bounded_semantic_feedback(result)

    assert result_dict["category_feedback"] == canonical
    assert result_dict["category_feedback_omitted"] == result.category_feedback_omitted
    assert adapter_dict["category_feedback"] == canonical
    assert adapter_dict["category_feedback_omitted"] == result.category_feedback_omitted


# --- End-to-end: shared creation carries the same feedback as existing writes ---

_EXISTING_ID = "00000000-0000-4000-8000-0000000000a1"
_CANDIDATE_ID = "00000000-0000-4000-8000-0000000000a2"
_EXISTING_PATH = "Knowledge Base/Notes/Insights/existing.md"
_CANDIDATE_PATH = "Knowledge Base/Notes/Insights/candidate.md"
_CONSTRAINTS_BODY = (
    "Body.\n\n"
    "## Observations\n\n"
    "- [constraints] Keep retry windows bounded #code\n\n"
    "## Relations\n"
)


def _compiled_source(page_id: str, *, title: str) -> str:
    return (
        "---\n"
        f"title: {title}\n"
        "type: insight\n"
        "status: active\n"
        f"exomem_id: {page_id}\n"
        "---\n\n"
        f"{_CONSTRAINTS_BODY}"
    )


def _feedback_fields(
    feedback: tuple[semantic_contract.CategoryFeedback, ...],
) -> list[dict[str, Any]]:
    # The per-unit `unit_ref` embeds the page identity/path, an unrelated
    # generated ID that legitimately differs between two distinct pages; the
    # advisory category fields themselves must not.
    return [
        {key: value for key, value in entry.as_dict().items() if key != "unit_ref"}
        for entry in feedback
    ]


def test_shared_creation_feedback_matches_existing_page_writes(tmp_path: Path) -> None:
    existing_source = _compiled_source(_EXISTING_ID, title="Existing")
    existing_path = tmp_path / _EXISTING_PATH
    existing_path.parent.mkdir(parents=True, exist_ok=True)
    existing_path.write_text(existing_source, encoding="utf-8", newline="")

    existing = semantic_writes.preflight_existing(
        tmp_path,
        path=_EXISTING_PATH,
        after_source=existing_source,
        operation="edit",
    )
    existing_result = existing.contract_result

    candidate_source = _compiled_source(_CANDIDATE_ID, title="Candidate")
    validation = relation_review.validate_creation_draft(
        tmp_path,
        path=_CANDIDATE_PATH,
        source=candidate_source,
        draft_id=_CANDIDATE_ID,
        operation="create",
    )
    preflight_result = validation.contract_result

    expected = [
        {
            "authored": "constraints",
            "normalized": "constraints",
            "canonical": "constraint",
            "status": "alias",
            "replacement": None,
        }
    ]
    # The creation preflight is the exact seam that previously threaded no
    # language registry into `semantic_contract.evaluate`, so its feedback came
    # back empty; it must now match the existing-page write field-for-field.
    assert _feedback_fields(existing_result.category_feedback) == expected
    assert _feedback_fields(preflight_result.category_feedback) == expected
    assert (
        preflight_result.category_feedback_omitted
        == existing_result.category_feedback_omitted
        == 0
    )

    # Commit replays the same shared leaf; its serialized adapter projection
    # must carry identical category feedback and omission count.
    commit = relation_review.commit_creation_draft(
        tmp_path,
        path=_CANDIDATE_PATH,
        source=candidate_source,
        draft_id=_CANDIDATE_ID,
        operation="create",
        relation_disposition="reviewed_none",
        relation_review_hash=validation.draft_hash,
        relation_review_reason="No qualifying typed relation yet",
    )
    commit_dict = semantic_writes._bounded_semantic_feedback(commit.contract_result)
    existing_dict = semantic_writes._bounded_semantic_feedback(existing_result)

    def _serialized(value: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {key: item for key, item in entry.items() if key != "unit_ref"}
            for entry in value["category_feedback"]
        ]

    assert _serialized(commit_dict) == _serialized(existing_dict) == expected
    assert (
        commit_dict["category_feedback_omitted"]
        == existing_dict["category_feedback_omitted"]
        == 0
    )


# --- Real cross-surface parity: MCP, REST, and CLI public write adapters ------

_CROSS_SURFACE_REST_KEY = "synthetic-test-key"
_ALIAS_NOTE_BODY = (
    "# Alias advisory feedback\n\n"
    "## Observations\n\n"
    "- [constraints] Keep retry windows bounded #code\n"
)
_ALIAS_FEEDBACK_FIELDS = {
    "authored": "constraints",
    "normalized": "constraints",
    "canonical": "constraint",
    "status": "alias",
    "replacement": None,
}


def _cross_surface_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    vault = tmp_path / "empty-vault"
    shutil.copytree(
        REPO_ROOT / "src" / "exomem" / "_scaffold" / "_Schema",
        vault / "Knowledge Base" / "_Schema",
    )
    monkeypatch.setattr(server, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "leases"))
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_RELEVANCE_CHECK", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_CLIP", "1")
    monkeypatch.setenv("EXOMEM_REST_API_KEY", _CROSS_SURFACE_REST_KEY)
    return vault, server.build_server(require_auth=False)


def _mcp_call(mcp, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result = asyncio.run(mcp.call_tool(name, arguments, run_middleware=False))
    if isinstance(result.structured_content, dict):
        return result.structured_content
    return json.loads(result.content[0].text)


def _category_fields(feedback: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # `unit_ref` embeds a per-write generated page identity/path that legitimately
    # differs between calls; the advisory category fields themselves must not.
    return [
        {key: value for key, value in entry.items() if key != "unit_ref"}
        for entry in feedback
    ]


def test_alias_feedback_is_identical_across_mcp_rest_and_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vault, mcp = _cross_surface_server(tmp_path, monkeypatch)
    preflight_args = {
        "note_type": "insight",
        "title": "Alias advisory feedback",
        "content": _ALIAS_NOTE_BODY,
        "validate_only": True,
    }
    expected = [_ALIAS_FEEDBACK_FIELDS]

    # --- MCP public adapter: preflight the creation. ---
    mcp_result = _mcp_call(mcp, "remember", preflight_args)
    assert mcp_result["mutated"] is False
    mcp_feedback = _category_fields(mcp_result["contract_result"]["category_feedback"])
    assert mcp_feedback == expected

    # --- REST public adapter: the same preflight over HTTP. ---
    client = TestClient(mcp.http_app())
    response = client.post(
        "/api/remember",
        json=preflight_args,
        headers={"Authorization": f"Bearer {_CROSS_SURFACE_REST_KEY}"},
    )
    assert response.status_code == 200, response.text
    rest_data = response.json()["data"]
    assert rest_data["mutated"] is False
    rest_feedback = _category_fields(rest_data["contract_result"]["category_feedback"])
    assert rest_feedback == expected

    # --- CLI public adapter: the same preflight through argv. `validate_only` is
    # a non-required remember param, so it is set through the --field escape. ---
    code = main(
        [
            "remember",
            "--content", _ALIAS_NOTE_BODY,
            "--title", "Alias advisory feedback",
            "--field", "note_type=insight",
            "--field", "validate_only=true",
            "--json",
        ]
    )
    assert code == 0
    cli_payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert cli_payload["success"] is True
    cli_data = cli_payload["data"]
    assert cli_data["mutated"] is False
    cli_feedback = _category_fields(cli_data["contract_result"]["category_feedback"])
    assert cli_feedback == expected

    # Every public adapter derives byte-identical canonical advisory feedback.
    assert mcp_feedback == rest_feedback == cli_feedback == expected

    # --- Commit through the MCP adapter: the alias is advisory and never
    # auto-applied, so the stored category text keeps the authored `constraints`. ---
    commit_args = {
        "note_type": "insight",
        "title": "Alias advisory feedback",
        "content": _ALIAS_NOTE_BODY,
        "draft_id": mcp_result["draft_id"],
        "draft_hash": mcp_result["draft_hash"],
        "draft_token": mcp_result["draft_token"],
    }
    committed = _mcp_call(mcp, "remember", commit_args)
    assert committed["mutated"] is True
    written = (vault / committed["path"]).read_text(encoding="utf-8")
    assert "[constraints]" in written
    assert "[constraint]" not in written
