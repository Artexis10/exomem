from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from exomem import (
    commands,
    index_sync,
    reconcile,
    semantic_index,
    semantic_language_registry,
    vault,
)

PAGE_ID = "00000000-0000-4000-8000-000000000081"
PAGE = "Knowledge Base/Notes/Insights/observe.md"


def _page_source(body: str = "# Observe\n\nExisting prose.\n") -> str:
    return (
        "---\n"
        "title: Observe\n"
        "type: insight\n"
        "status: active\n"
        f"exomem_id: {PAGE_ID}\n"
        "updated: 2026-07-15\n"
        "---\n\n"
        f"{body.rstrip()}\n"
    )


def _write_page(root: Path, *, rel: str = PAGE, source: str | None = None) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source or _page_source(), encoding="utf-8")
    return path


def test_add_compact_observation_is_canonical_addressable_and_indexed(
    tmp_path: Path,
) -> None:
    page = _write_page(tmp_path)

    result = commands.op_observe_memory(
        tmp_path,
        path=PAGE,
        operation="add",
        category="Config Rule",
        content="Keep WAL enabled",
        tags=["sqlite", "runtime/db"],
        context="desktop startup",
    )

    source = page.read_text(encoding="utf-8")
    assert source.count("## Observations") == 1
    assert (
        "- [config_rule] Keep WAL enabled #sqlite #runtime/db "
        "(desktop startup) ^obs-" in source
    )
    assert result["operation"] == "add"
    assert result["mutated"] is True
    assert result["unit"]["unit_ref"] == result["unit_ref"]
    assert result["unit_ref"].startswith(f"exomem://memory/{PAGE_ID}#obs-")
    state = semantic_index.current_parent_index_state(tmp_path, PAGE)
    assert state.document.resolve_unit(result["unit_ref"]).status == "found"
    assert result["semantic"]["index"]["requested_paths"][-1] == PAGE
    assert all(
        component["outcome"] in {"accepted", "completed", "deferred", "degraded"}
        for component in result["semantic"]["index"]["components"]
    )


def test_validate_assigns_anchor_from_canonical_rendered_fields(tmp_path: Path) -> None:
    _write_page(tmp_path)

    padded = commands.op_observe_memory(
        tmp_path,
        path=PAGE,
        operation="validate",
        category="rule",
        content="  Stable canonical content  ",
    )
    canonical = commands.op_observe_memory(
        tmp_path,
        path=PAGE,
        operation="validate",
        category="rule",
        content="Stable canonical content",
    )

    assert padded["unit"]["content"] == "Stable canonical content"
    assert padded["unit"]["anchor"] == canonical["unit"]["anchor"]


def test_validate_returns_normalized_unit_without_markdown_or_sidecar_writes(
    tmp_path: Path,
) -> None:
    page = _write_page(tmp_path)
    before = page.read_bytes()

    result = commands.op_observe_memory(
        tmp_path,
        path=PAGE,
        operation="validate",
        category="Rule",
        content="Validation is side-effect free",
    )

    assert result["operation"] == "validate"
    assert result["mutated"] is False
    assert result["unit"]["category_key"] == "rule"
    assert result["semantic"]["mutated"] is False
    assert page.read_bytes() == before
    assert not (tmp_path / "Knowledge Base" / "_system").exists()


def test_update_and_remove_use_exact_unit_spans_and_preserve_unrelated_markdown(
    tmp_path: Path,
) -> None:
    page = _write_page(
        tmp_path,
        source=_page_source(
            "# Observe\n\nBefore.\n\n## Observations\n\n"
            "- [rule] First unit ^first\n"
            "- [rule] Second unit ^second\n\nAfter.\n"
        ),
    )
    state = semantic_index.current_parent_index_state(tmp_path, PAGE)
    first = next(unit for unit in state.document.units if unit.anchor == "first")
    second = next(unit for unit in state.document.units if unit.anchor == "second")

    updated = commands.op_observe_memory(
        tmp_path,
        path=PAGE,
        operation="update",
        unit_ref=first.unit_ref,
        expected_fingerprint=first.fingerprint,
        expected_hash=state.parent_source_hash,
        category="Decision",
        content="First unit changed",
    )
    assert updated["unit_ref"].endswith("#first")
    source = page.read_text(encoding="utf-8")
    assert "Before." in source and "After." in source
    assert "- [decision] First unit changed ^first" in source
    assert "- [rule] Second unit ^second" in source

    removed = commands.op_observe_memory(
        tmp_path,
        path=PAGE,
        operation="remove",
        unit_ref=second.unit_ref,
        expected_fingerprint=second.fingerprint,
        expected_hash=updated["after_hash"],
    )
    assert removed["removed_unit_ref"] == second.unit_ref
    source = page.read_text(encoding="utf-8")
    assert "Second unit" not in source
    assert "Before." in source and "After." in source


@pytest.mark.parametrize("guard", ["parent", "unit"])
def test_stale_update_guard_changes_neither_markdown_nor_indexes(
    tmp_path: Path,
    guard: str,
) -> None:
    page = _write_page(
        tmp_path,
        source=_page_source("## Observations\n\n- [rule] Current ^current\n"),
    )
    state = semantic_index.current_parent_index_state(tmp_path, PAGE)
    unit = state.document.units[0]
    before = page.read_bytes()

    with pytest.raises(ValueError, match="STALE_"):
        commands.op_observe_memory(
            tmp_path,
            path=PAGE,
            operation="update",
            unit_ref=unit.unit_ref,
            expected_fingerprint=(
                "0" * 64 if guard == "unit" else unit.fingerprint
            ),
            expected_hash=("0" * 64 if guard == "parent" else vault.content_hash(before.decode())),
            category="rule",
            content="Should not land",
        )

    assert page.read_bytes() == before
    after_state = semantic_index.current_parent_index_state(tmp_path, PAGE)
    assert after_state.parent_source_hash == state.parent_source_hash


@pytest.mark.parametrize("operation", ["update", "remove"])
@pytest.mark.parametrize("missing_guard", ["expected_hash", "expected_fingerprint"])
def test_update_and_remove_require_complete_lost_update_guards(
    tmp_path: Path,
    operation: str,
    missing_guard: str,
) -> None:
    page = _write_page(
        tmp_path,
        source=_page_source("## Observations\n\n- [rule] Current ^current\n"),
    )
    state = semantic_index.current_parent_index_state(tmp_path, PAGE)
    unit = state.document.units[0]
    before = page.read_bytes()
    arguments = {
        "path": PAGE,
        "operation": operation,
        "unit_ref": unit.unit_ref,
        "expected_hash": state.parent_source_hash,
        "expected_fingerprint": unit.fingerprint,
    }
    if operation == "update":
        arguments.update(category="rule", content="Changed")
    del arguments[missing_guard]

    with pytest.raises(ValueError, match="DRIFT_GUARDS_REQUIRED"):
        commands.op_observe_memory(tmp_path, **arguments)

    assert page.read_bytes() == before


def test_anonymous_duplicate_selection_is_occurrence_exact(tmp_path: Path) -> None:
    page = _write_page(
        tmp_path,
        source=_page_source(
            "## Observations\n\n- [rule] Same\n- [rule] Same\n- [rule] Tail\n"
        ),
    )
    state = semantic_index.current_parent_index_state(tmp_path, PAGE)
    duplicates = [unit for unit in state.document.units if unit.content == "Same"]
    assert len(duplicates) == 2
    assert duplicates[0].unit_ref != duplicates[1].unit_ref

    commands.op_observe_memory(
        tmp_path,
        path=PAGE,
        operation="update",
        unit_ref=duplicates[1].unit_ref,
        expected_fingerprint=duplicates[1].fingerprint,
        expected_hash=state.parent_source_hash,
        category="rule",
        content="Second only",
    )

    source = page.read_text(encoding="utf-8")
    assert source.count("- [rule] Same") == 1
    assert source.count("- [rule] Second only") == 1
    assert "- [rule] Tail" in source


def test_rich_authoring_and_compact_relation_rejection(tmp_path: Path) -> None:
    page = _write_page(tmp_path)
    relation = [{"kind": "supports", "target": "[[Observe]]"}]

    with pytest.raises(ValueError, match="COMPACT_RELATIONS_REQUIRE_RICH_KIND"):
        commands.op_observe_memory(
            tmp_path,
            path=PAGE,
            operation="add",
            category="evidence",
            content="Typed relation needs rich form",
            relations=relation,
        )

    assert "Typed relation needs rich form" not in page.read_text(encoding="utf-8")

    result = commands.op_observe_memory(
        tmp_path,
        path=PAGE,
        operation="validate",
        kind="claim",
        category="architecture",
        content="SQLite is the local source of truth.",
        tags=["storage"],
        context="desktop",
        relations=relation,
    )
    assert result["unit"]["form"] == "rich"
    assert result["unit"]["kind"] == "claim"
    assert result["unit"]["metadata"]["category"] == "architecture"
    assert result["unit"]["relations"][0]["kind"] == "supports"


def test_rich_update_relations_still_require_explicit_kind(tmp_path: Path) -> None:
    page = _write_page(
        tmp_path,
        source=_page_source(
            "## Claim\n- category: rule\n- id: claim-one\n\nCurrent claim.\n"
        ),
    )
    state = semantic_index.current_parent_index_state(tmp_path, PAGE)
    unit = state.document.units[0]
    before = page.read_bytes()

    with pytest.raises(ValueError, match="COMPACT_RELATIONS_REQUIRE_RICH_KIND"):
        commands.op_observe_memory(
            tmp_path,
            path=PAGE,
            operation="update",
            unit_ref=unit.unit_ref,
            expected_fingerprint=unit.fingerprint,
            expected_hash=state.parent_source_hash,
            category="rule",
            content="Changed claim.",
            relations=[{"kind": "supports", "target": "[[Observe]]"}],
        )

    assert page.read_bytes() == before


def test_rich_update_preserves_separator_outside_exact_unit_span(tmp_path: Path) -> None:
    page = _write_page(
        tmp_path,
        source=_page_source(
            "## Claim\n- category: rule\n- id: claim-one\n\nCurrent claim.\n\n"
            "## Decision\n- category: rule\n- id: decision-one\n\nKeep this.\n"
        ),
    )
    state = semantic_index.current_parent_index_state(tmp_path, PAGE)
    unit = state.document.units[0]

    commands.op_observe_memory(
        tmp_path,
        path=PAGE,
        operation="update",
        unit_ref=unit.unit_ref,
        expected_fingerprint=unit.fingerprint,
        expected_hash=state.parent_source_hash,
        kind="claim",
        category="rule",
        content="Updated claim.",
    )

    source = page.read_text(encoding="utf-8")
    assert "Updated claim.\n\n## Decision" in source
    assert "## Decision\n- category: rule\n- id: decision-one\n\nKeep this." in source


def test_add_rich_unit_commits_canonical_metadata(tmp_path: Path) -> None:
    page = _write_page(tmp_path)

    result = commands.op_observe_memory(
        tmp_path,
        path=PAGE,
        operation="add",
        kind="decision",
        category="Architecture Rule",
        content="Use one guarded semantic writer.",
        tags=["storage", "safety"],
        context="core product",
    )

    source = page.read_text(encoding="utf-8")
    assert "## Decision\n- category: architecture_rule\n- id: unit-" in source
    assert "- tags: storage, safety\n- context: core product" in source
    assert result["unit"]["form"] == "rich"
    assert result["unit"]["metadata"]["tags"] == "storage, safety"


def test_rich_kind_resolution_honors_any_attached_project_scope(
    tmp_path: Path,
) -> None:
    semantic_language_registry.save_registry(
        tmp_path,
        {
            "schema_version": 1,
            "categories": {},
            "kinds": {
                "protocol": {
                    "description": "A project-specific repeatable protocol",
                    "aliases": ["playbook"],
                    "scope": {"projects": ["alpha"]},
                }
            },
        },
    )
    _write_page(
        tmp_path,
        source=_page_source().replace(
            "status: active\n", "status: active\nprojects: [beta, alpha]\n"
        ),
    )

    result = commands.op_observe_memory(
        tmp_path,
        path=PAGE,
        operation="validate",
        kind="playbook",
        category="workflow",
        content="Repeat the guarded sequence.",
    )

    assert result["unit"]["kind"] == "protocol"
    assert result["unit"]["title"] == "Protocol"


def test_rich_kind_resolution_rejects_out_of_scope_project(tmp_path: Path) -> None:
    semantic_language_registry.save_registry(
        tmp_path,
        {
            "schema_version": 1,
            "categories": {},
            "kinds": {
                "protocol": {
                    "description": "A project-specific repeatable protocol",
                    "scope": {"projects": ["alpha"]},
                }
            },
        },
    )
    page = _write_page(
        tmp_path,
        source=_page_source().replace(
            "status: active\n", "status: active\nproject: beta\n"
        ),
    )
    before = page.read_bytes()

    with pytest.raises(ValueError, match="UNSUPPORTED_SEMANTIC_KIND"):
        commands.op_observe_memory(
            tmp_path,
            path=PAGE,
            operation="validate",
            kind="protocol",
            category="workflow",
            content="Must remain out of scope.",
        )

    assert page.read_bytes() == before


@pytest.mark.parametrize(
    "content",
    ["Looks like an implicit #tag", "Looks like implicit context (desktop)"],
)
def test_compact_content_cannot_be_silently_reinterpreted(
    tmp_path: Path,
    content: str,
) -> None:
    page = _write_page(tmp_path)
    before = page.read_bytes()

    with pytest.raises(ValueError, match="AMBIGUOUS_SEMANTIC_UNIT_CONTENT"):
        commands.op_observe_memory(
            tmp_path,
            path=PAGE,
            operation="add",
            category="rule",
            content=content,
        )

    assert page.read_bytes() == before


def test_validate_then_reviewed_none_commit_round_trips_exact_transition(
    tmp_path: Path,
) -> None:
    _write_page(tmp_path)
    first_preview = commands.op_observe_memory(
        tmp_path,
        path=PAGE,
        operation="validate",
        category="rule",
        content="Activate the page lifecycle",
    )
    first_semantic = first_preview["semantic"]
    first = commands.op_observe_memory(
        tmp_path,
        path=PAGE,
        operation="add",
        category="rule",
        content="Activate the page lifecycle",
        transition_token=first_semantic["transition_token"],
        relation_disposition="reviewed_none",
        relation_review_hash=first_semantic["transition_hash"],
        relation_review_reason="No honest relation exists for the first observation",
    )
    proposal = {
        "path": PAGE,
        "category": "rule",
        "content": "Second disconnected observation",
        "expected_hash": first["after_hash"],
    }

    preview = commands.op_observe_memory(tmp_path, **proposal, operation="validate")
    semantic = preview["semantic"]
    assert semantic["contract_result"]["should_block"] is True

    committed = commands.op_observe_memory(
        tmp_path,
        **proposal,
        operation="add",
        transition_token=semantic["transition_token"],
        relation_disposition="reviewed_none",
        relation_review_hash=semantic["transition_hash"],
        relation_review_reason="No honest relation exists for this atomic observation",
    )
    assert committed["mutated"] is True
    assert committed["semantic"]["contract_result"]["should_block"] is False


@pytest.mark.parametrize("tier", ["readonly", "excluded"])
def test_validate_refuses_access_policy_boundaries(tmp_path: Path, tier: str) -> None:
    page = _write_page(tmp_path)
    policy = tmp_path / "Knowledge Base" / "_access.yaml"
    policy.write_text(f"{tier}:\n  - Notes/Insights\n", encoding="utf-8")
    before = page.read_bytes()

    with pytest.raises(ValueError, match="OBSERVE_TARGET_NOT_WRITABLE_COMPILED_PAGE"):
        commands.op_observe_memory(
            tmp_path,
            path=PAGE,
            operation="validate",
            category="rule",
            content="Policy refusal",
        )

    assert page.read_bytes() == before


@pytest.mark.parametrize(
    ("rel", "status", "page_type", "applicability"),
    [
        (PAGE, "draft", "insight", "structural"),
        (
            "Knowledge Base/Notes/Research/alpha/system-overview.md",
            "active",
            "research-note",
            "full",
        ),
    ],
)
def test_inactive_and_active_compiled_pages_remain_writable(
    tmp_path: Path,
    rel: str,
    status: str,
    page_type: str,
    applicability: str,
) -> None:
    page = _write_page(
        tmp_path,
        rel=rel,
        source=(
            _page_source()
            .replace("status: active", f"status: {status}")
            .replace("type: insight", f"type: {page_type}")
        ),
    )

    result = commands.op_observe_memory(
        tmp_path,
        path=rel,
        operation="add",
        category="rule",
        content="Structured units remain writable outside the active relation corpus",
    )

    assert "Structured units remain writable" in page.read_text(encoding="utf-8")
    assert result["semantic"]["applicability"] == applicability


def test_index_dispatch_failure_preserves_markdown_and_reports_reconcile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _write_page(tmp_path)
    original = index_sync.upsert_after_write

    def fail_dispatch(*args, **kwargs):  # noqa: ANN002, ANN003, ARG001
        raise RuntimeError("simulated outer index failure")

    monkeypatch.setattr(index_sync, "upsert_after_write", fail_dispatch)
    result = commands.op_observe_memory(
        tmp_path,
        path=PAGE,
        operation="add",
        category="rule",
        content="Markdown survives index failure",
    )

    assert "Markdown survives index failure" in page.read_text(encoding="utf-8")
    report = result["semantic"]["index"]
    assert report["reconcile_required"] is True
    assert {item["outcome"] for item in report["components"]} == {"degraded"}

    monkeypatch.setattr(index_sync, "upsert_after_write", original)
    repaired = reconcile.reconcile(tmp_path)
    assert repaired.semantic_unit_indexes_status == "current"


@pytest.mark.parametrize(
    "rel",
    [
        "Knowledge Base/Sources/raw.md",
        "Knowledge Base/Evidence/proof.md",
        "Outside/curated.md",
    ],
)
def test_observe_refuses_immutable_or_outside_compiled_targets(
    tmp_path: Path,
    rel: str,
) -> None:
    page = _write_page(tmp_path, rel=rel)

    with pytest.raises(ValueError):
        commands.op_observe_memory(
            tmp_path,
            path=rel,
            operation="add",
            category="rule",
            content="Must not land",
        )

    assert "Must not land" not in page.read_text(encoding="utf-8")


def test_observe_accepts_parent_memory_reference(tmp_path: Path) -> None:
    _write_page(tmp_path)

    result = commands.op_observe_memory(
        tmp_path,
        path=f"exomem://memory/{uuid.UUID(PAGE_ID)}",
        operation="add",
        category="rule",
        content="Reference-resolved parent",
    )

    assert result["path"] == PAGE
    assert result["unit_ref"].startswith(f"exomem://memory/{PAGE_ID}#")


def test_observe_accepts_legacy_vault_reference_inside_governed_kb(
    tmp_path: Path,
) -> None:
    _write_page(tmp_path)

    result = commands.op_observe_memory(
        tmp_path,
        path="exomem://vault/Knowledge%20Base/Notes/Insights/observe.md",
        operation="validate",
        category="rule",
        content="Legacy reference remains usable",
    )

    assert result["path"] == PAGE


@pytest.mark.parametrize(
    "unsafe_path",
    [
        "exomem://vault/Outside/curated.md",
        "/Notes/Insights/observe.md",
    ],
)
def test_explicit_outside_parent_cannot_alias_a_governed_page(
    tmp_path: Path,
    unsafe_path: str,
) -> None:
    outside = _write_page(tmp_path, rel="Outside/curated.md")
    collision = _write_page(
        tmp_path,
        rel="Knowledge Base/Outside/curated.md",
        source=_page_source().replace("Observe", "Collision"),
    )
    page = _write_page(tmp_path)
    before = {target: target.read_bytes() for target in (outside, collision, page)}

    with pytest.raises(ValueError, match="INVALID_PATH"):
        commands.op_observe_memory(
            tmp_path,
            path=unsafe_path,
            operation="add",
            category="rule",
            content="Must not alias into the KB",
        )

    assert {target: target.read_bytes() for target in before} == before


def test_reference_validate_creates_no_reference_or_semantic_sidecars(
    tmp_path: Path,
) -> None:
    page = _write_page(tmp_path)
    before = page.read_bytes()

    result = commands.op_observe_memory(
        tmp_path,
        path=f"exomem://memory/{PAGE_ID}",
        operation="validate",
        category="rule",
        content="Reference validation stays pure",
    )

    assert result["mutated"] is False
    assert page.read_bytes() == before
    assert not (tmp_path / "Knowledge Base" / ".refs.sqlite").exists()
    assert not (tmp_path / "Knowledge Base" / ".lexical.sqlite").exists()


def test_add_preserves_unrelated_body_whitespace_and_logs_once(tmp_path: Path) -> None:
    source = _page_source("# Observe\n\nKeep trailing space.").rstrip("\n") + "  \n\n\n"
    page = _write_page(tmp_path, source=source)
    log = tmp_path / "Knowledge Base" / "log.md"
    log.write_text("# Log\n", encoding="utf-8")

    commands.op_observe_memory(
        tmp_path,
        path=PAGE,
        operation="add",
        category="rule",
        content="Logged structured mutation",
    )

    after = page.read_text(encoding="utf-8")
    assert "Keep trailing space.  \n\n\n## Observations" in after
    log_text = log.read_text(encoding="utf-8")
    assert log_text.count("## [") == 1
    assert "] observe | Notes/Insights/observe" in log_text
