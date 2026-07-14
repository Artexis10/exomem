from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from exomem import (
    activation_manifest,
    index_sync,
    memory_schema,
    relation_review,
    semantic_contract,
    semantic_writes,
    vault,
)
from exomem import (
    append_to_file as append_module,
)
from exomem import (
    create_file as create_file_module,
)
from exomem import (
    edit as edit_module,
)
from exomem import (
    multi_edit as multi_edit_module,
)
from exomem import (
    set_frontmatter_field as set_frontmatter_module,
)
from exomem import (
    set_take as set_take_module,
)

_ID = "00000000-0000-4000-8000-000000000061"
_OTHER_ID = "00000000-0000-4000-8000-000000000062"
_TRANSITION_AB = "00000000-0000-4000-8000-0000000000ab"
_TRANSITION_BC = "00000000-0000-4000-8000-0000000000bc"
_TRANSITION_CB = "00000000-0000-4000-8000-0000000000cb"
_PAGE = "Knowledge Base/Notes/Insights/lifecycle.md"
_OTHER_PAGE = "Knowledge Base/Notes/Insights/lifecycle-moved.md"


def _source(body: str, *, page_id: str | None = _ID) -> str:
    identity = f"exomem_id: {page_id}\n" if page_id is not None else ""
    return (
        "---\n"
        "title: Lifecycle\n"
        "type: insight\n"
        "status: active\n"
        f"{identity}"
        "---\n\n"
        f"{body}\n\n"
        "## Relations\n"
    )


def _write(root: Path, rel: str, source: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


def _save_required_block_contract(
    root: Path, *, name: str, project: str, block: str, validation: str
) -> None:
    memory_schema.save_contract(
        root,
        memory_schema.MemoryContract(
            name=name,
            scope=memory_schema.ContractScope(
                project=project, page_type="insight"
            ),
            sample_size=1,
            fields={},
            blocks={block: {"required": True}},
            relations={},
            validation=validation,
        ).as_dict(),
    )


def _binding(path: str, source: str) -> relation_review.LifecyclePrimaryBinding:
    return relation_review.LifecyclePrimaryBinding(
        path=path,
        source_hash=vault.content_hash(source),
        review_fingerprint=semantic_contract.review_content_fingerprint(_ID, source),
    )


def _decision(source: str, *, reason: str = "No honest relation exists"):
    return relation_review.build_lifecycle_decision(
        page_identity=_ID,
        after_fingerprint=semantic_contract.review_content_fingerprint(_ID, source),
        reason=reason,
    )


def _prepared(
    before_source: str,
    after_source: str,
    *,
    transition_id: str,
    decision,
    before_path: str = _PAGE,
    after_path: str = _PAGE,
    token: str = "bounded-transition-token",
    carried_from=None,
):
    return relation_review.build_lifecycle_prepared_transition(
        transition_id=transition_id,
        operation="edit",
        page_identity=_ID,
        before_path=before_path,
        before_source_hash=vault.content_hash(before_source),
        after_path=after_path,
        after_source_hash=vault.content_hash(after_source),
        after_fingerprint=semantic_contract.review_content_fingerprint(_ID, after_source),
        decision=decision,
        transition_token=token,
        auxiliary_hash=hashlib.sha256(b"auxiliaries").hexdigest(),
        carried_from=carried_from,
    )


def _apply_plan(root: Path, plan: relation_review.LifecycleTransitionPlan) -> None:
    vault.batch_atomic_write(
        plan.writes,
        vault_root=root,
        required_guards=plan.required_guards,
    )


def test_existing_preflight_classifies_transition_without_mutation(tmp_path: Path) -> None:
    before = _source("A")
    page = _write(tmp_path, _PAGE, before)
    after = before.replace("A\n\n## Relations", "B\n\n## Relations")

    preflight = semantic_writes.preflight_existing(
        tmp_path,
        path=_PAGE,
        after_source=after,
        operation="edit",
        expected_before_hash=vault.content_hash(before),
    )

    assert preflight.applicability == "full"
    assert preflight.before.source_hash == vault.content_hash(before)
    assert preflight.after.source_hash == vault.content_hash(after)
    assert preflight.grandfathered is True
    assert preflight.mutated is False
    assert preflight.transition_token
    assert preflight.transition_hash == hashlib.sha256(
        preflight.transition_token.encode("utf-8")
    ).hexdigest()
    assert page.read_text(encoding="utf-8") == before
    assert not activation_manifest.manifest_path(tmp_path).exists()


def test_existing_preflight_returns_equivalent_findings_for_all_entry_operations(
    tmp_path: Path,
) -> None:
    before = _source("A")
    _write(tmp_path, _PAGE, before)
    after = before.replace("A\n\n## Relations", "B\n\n## Relations")

    results = [
        semantic_writes.preflight_existing(
            tmp_path,
            path=_PAGE,
            after_source=after,
            operation=operation,
            expected_before_hash=vault.content_hash(before),
        ).contract_result
        for operation in ("edit", "tier2_overwrite", "tier2_append")
    ]

    finding_shapes = [
        [(item.code, item.key, item.severity) for item in result.findings]
        for result in results
    ]
    assert finding_shapes[0] == finding_shapes[1] == finding_shapes[2]
    assert [result.should_block for result in results] == [False, False, False]


@pytest.mark.parametrize(
    ("before_status", "after_status", "expected_applicability", "grandfathered"),
    [
        ("active", "active", "full", True),
        ("active", "archived", "structural", True),
        ("draft", "draft", "structural", False),
        ("draft", "active", "full", False),
    ],
)
def test_existing_preflight_applies_the_lifecycle_matrix(
    tmp_path: Path,
    before_status: str,
    after_status: str,
    expected_applicability: str,
    grandfathered: bool,
) -> None:
    before = _source("A").replace("status: active", f"status: {before_status}")
    after = before.replace(f"status: {before_status}", f"status: {after_status}")
    _write(tmp_path, _PAGE, before)

    preflight = semantic_writes.preflight_existing(
        tmp_path,
        path=_PAGE,
        after_source=after,
        operation="edit",
        expected_before_hash=vault.content_hash(before),
    )

    assert preflight.applicability == expected_applicability
    assert preflight.grandfathered is grandfathered
    assert (_PAGE in preflight.after_corpus.eligible_compiled_paths) is (
        after_status == "active"
    )


def test_existing_preflight_rejects_an_unknown_operation_before_mutation(
    tmp_path: Path,
) -> None:
    source = _source("A")
    page = _write(tmp_path, _PAGE, source)

    with pytest.raises(semantic_writes.SemanticWriteError) as exc:
        semantic_writes.preflight_existing(
            tmp_path,
            path=_PAGE,
            after_source=source,
            operation="move",  # type: ignore[arg-type]
        )

    assert exc.value.code == "LIFECYCLE_TRANSITION_INVALID_OPERATION"
    assert page.read_text(encoding="utf-8") == source
    assert not activation_manifest.manifest_path(tmp_path).exists()


def test_existing_preflight_rejects_reviewed_none_for_inactive_result(
    tmp_path: Path,
) -> None:
    source = _source("Draft").replace("status: active", "status: draft")
    _write(tmp_path, _PAGE, source)
    preview = semantic_writes.preflight_existing(
        tmp_path,
        path=_PAGE,
        after_source=source.replace("Draft", "Still draft"),
        operation="edit",
    )

    with pytest.raises(semantic_writes.SemanticWriteError) as exc:
        semantic_writes.preflight_existing(
            tmp_path,
            path=_PAGE,
            after_source=source.replace("Draft", "Still draft"),
            operation="edit",
            transition_token=preview.transition_token,
            relation_disposition="reviewed_none",
            relation_review_hash=preview.transition_hash,
            relation_review_reason="Inactive pages do not receive review records",
        )

    assert exc.value.code == "INVALID_RELATION_REVIEW"
    assert not relation_review.lifecycle_prepared_path(tmp_path, _ID).exists()


def test_existing_commit_installs_boundary_and_commits_primary_last_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = _source("A")
    page = _write(tmp_path, _PAGE, before)
    after = before.replace("A\n\n## Relations", "B\n\n## Relations")
    preflight = semantic_writes.preflight_existing(
        tmp_path,
        path=_PAGE,
        after_source=after,
        operation="edit",
        expected_before_hash=vault.content_hash(before),
    )
    original_batch = vault.batch_atomic_write
    ordered_batches: list[list[str]] = []

    def capture_batch(writes, **kwargs):
        materialized = list(writes)
        ordered_batches.append(
            [item.path.relative_to(tmp_path).as_posix() for item in materialized]
        )
        return original_batch(materialized, **kwargs)

    fanouts: list[list[Path]] = []

    def one_report(_root: Path, paths: list[Path], **_kwargs):
        fanouts.append(paths)
        rels = tuple(path.relative_to(tmp_path).as_posix() for path in paths)
        return index_sync.IndexSyncReport("upsert", rels, rels, ())

    monkeypatch.setattr(semantic_writes.vault, "batch_atomic_write", capture_batch)
    monkeypatch.setattr(index_sync, "upsert_after_write", one_report)

    committed = semantic_writes.commit_existing(tmp_path, preflight=preflight)

    assert page.read_text(encoding="utf-8") == after
    assert activation_manifest.manifest_path(tmp_path).exists()
    assert relation_review.lifecycle_prepared_path(tmp_path, _ID).exists()
    assert ordered_batches[-1][-1] == _PAGE
    assert ordered_batches[-1][0].endswith("/prepared.json")
    assert len(fanouts) == 2  # manifest install, then the actual existing-page commit
    assert committed.index_report is not None
    assert committed.index_report.requested_paths[-1] == _PAGE


def test_material_edit_requires_and_commits_exact_reviewed_none_transition(
    tmp_path: Path,
) -> None:
    before = _source("A")
    page = _write(tmp_path, _PAGE, before)
    current_decision = _decision(before, reason="Current state reviewed")
    decision_path = relation_review.lifecycle_decision_path(
        tmp_path, _ID, current_decision.after_fingerprint
    )
    decision_path.parent.mkdir(parents=True, exist_ok=True)
    decision_path.write_text(
        relation_review.serialize_lifecycle_decision(current_decision),
        encoding="utf-8",
    )
    after = before.replace("A\n\n## Relations", "B\n\n## Relations")

    preview = semantic_writes.preflight_existing(
        tmp_path,
        path=_PAGE,
        after_source=after,
        operation="edit",
        expected_before_hash=vault.content_hash(before),
    )
    assert preview.contract_result.should_block is True

    reviewed = semantic_writes.preflight_existing(
        tmp_path,
        path=_PAGE,
        after_source=after,
        operation="edit",
        expected_before_hash=vault.content_hash(before),
        transition_token=preview.transition_token,
        relation_disposition="reviewed_none",
        relation_review_hash=preview.transition_hash,
        relation_review_reason="No honest relation exists for the revised page",
    )

    assert reviewed.contract_result.should_block is False
    assert reviewed.requested_decision is not None
    assert reviewed.requested_decision.after_fingerprint == reviewed.after.review_fingerprint
    committed = semantic_writes.commit_existing(tmp_path, preflight=reviewed)
    assert committed.mutated is True
    assert page.read_text(encoding="utf-8") == after
    loaded = relation_review.load_lifecycle_decision(
        tmp_path, _ID, reviewed.after.review_fingerprint
    )
    assert loaded == reviewed.requested_decision


def test_surgical_validate_only_adds_semantic_preflight_without_mutation(
    tmp_path: Path,
) -> None:
    source = _source("A")
    page = _write(tmp_path, _PAGE, source)

    result = edit_module.edit(
        tmp_path,
        path=_PAGE,
        why="preview semantic lifecycle",
        old_string="A",
        new_string="B",
        validate_only=True,
        today=dt.date(2026, 7, 14),
    )

    assert result.match_count == 1
    assert result.semantic is not None
    assert result.semantic["operation"] == "edit"
    assert result.semantic["mutated"] is False
    assert result.semantic["transition_token"]
    assert page.read_text(encoding="utf-8") == source
    assert not activation_manifest.manifest_path(tmp_path).exists()


def test_surgical_edit_commits_through_existing_semantic_coordinator(
    tmp_path: Path,
) -> None:
    source = _source("A")
    page = _write(tmp_path, _PAGE, source)

    result = edit_module.edit(
        tmp_path,
        path=_PAGE,
        why="exercise semantic lifecycle",
        old_string="A",
        new_string="B",
        today=dt.date(2026, 7, 14),
    )

    assert result.path == _PAGE
    assert result.semantic is not None
    assert result.semantic["operation"] == "edit"
    assert result.semantic["mutated"] is True
    assert "B" in page.read_text(encoding="utf-8")
    assert activation_manifest.manifest_path(tmp_path).exists()
    assert relation_review.lifecycle_prepared_path(tmp_path, _ID).exists()
    assert set(result.as_dict()) == {"path", "warnings", "semantic"}


def test_multi_edit_validate_and_commit_preserve_shape_with_semantic_feedback(
    tmp_path: Path,
) -> None:
    source = _source("A\n\nC")
    _write(tmp_path, _PAGE, source)
    edits = [
        {"old_string": "A", "new_string": "B"},
        {"old_string": "C", "new_string": "D"},
    ]

    preview = multi_edit_module.multi_edit(
        tmp_path,
        path=_PAGE,
        why="preview batch",
        edits=edits,
        validate_only=True,
        today=dt.date(2026, 7, 14),
    )
    assert preview.edits == [
        {"index": 0, "match_count": 1, "replace_all": False},
        {"index": 1, "match_count": 1, "replace_all": False},
    ]
    assert preview.semantic is not None
    assert preview.semantic["mutated"] is False

    committed = multi_edit_module.multi_edit(
        tmp_path,
        path=_PAGE,
        why="commit batch",
        edits=edits,
        today=dt.date(2026, 7, 14),
    )
    assert committed.edits_applied == 2
    assert committed.semantic is not None
    assert committed.semantic["mutated"] is True
    assert set(committed.as_dict()) == {
        "path",
        "edits_applied",
        "warnings",
        "semantic",
    }


def test_set_take_propagates_edit_semantic_feedback_unchanged(tmp_path: Path) -> None:
    source = _source("- Film (2026) [take: ]")
    _write(tmp_path, _PAGE, source)

    result = set_take_module.set_take(
        tmp_path,
        path=_PAGE,
        row_key="Film (2026)",
        take="Sharp.",
        why="record take",
        today=dt.date(2026, 7, 14),
    )

    assert result.semantic is not None
    assert result.semantic["operation"] == "edit"
    assert result.semantic["mutated"] is True
    assert set(result.as_dict()) == {"path", "row", "warnings", "semantic"}


@pytest.mark.parametrize("validation", ["strict", "warn", "off"])
def test_real_edit_honors_saved_contract_validation_mode(
    tmp_path: Path, validation: str
) -> None:
    _save_required_block_contract(
        tmp_path,
        name=f"alpha-{validation}",
        project="alpha",
        block="finding",
        validation=validation,
    )
    source = _source("## Finding\n\nRequired conclusion.").replace(
        "status: active\n", "status: active\nproject: alpha\n"
    )
    page = _write(tmp_path, _PAGE, source)

    if validation == "strict":
        with pytest.raises(edit_module.EditError) as exc:
            edit_module.edit(
                tmp_path,
                path=_PAGE,
                why="remove required block",
                new_body="No structured block remains.",
                today=dt.date(2026, 7, 14),
            )
        assert exc.value.code == "SEMANTIC_CONTRACT_BLOCKED"
        assert page.read_text(encoding="utf-8") == source
        return

    result = edit_module.edit(
        tmp_path,
        path=_PAGE,
        why="remove required block",
        new_body="No structured block remains.",
        today=dt.date(2026, 7, 14),
    )
    assert result.semantic is not None
    findings = result.semantic["contract_result"]["findings"]
    contract_findings = [
        item for item in findings if item["code"].startswith("CONTRACT_")
    ]
    if validation == "warn":
        assert [item["code"] for item in contract_findings] == [
            "CONTRACT_REQUIRED_BLOCK"
        ]
        assert contract_findings[0]["severity"] == "warning"
    else:
        assert contract_findings == []


def test_real_edit_resolves_multi_project_saved_contracts(tmp_path: Path) -> None:
    _save_required_block_contract(
        tmp_path,
        name="alpha-required",
        project="alpha",
        block="finding",
        validation="strict",
    )
    _save_required_block_contract(
        tmp_path,
        name="beta-required",
        project="beta",
        block="decision",
        validation="strict",
    )
    source = _source(
        "## Finding\n\nAlpha.\n\n## Decision\n\nBeta."
    ).replace("status: active\n", "status: active\nprojects: [alpha, beta]\n")
    page = _write(tmp_path, _PAGE, source)

    with pytest.raises(edit_module.EditError) as exc:
        edit_module.edit(
            tmp_path,
            path=_PAGE,
            why="remove beta block",
            new_body="## Finding\n\nAlpha remains.",
            today=dt.date(2026, 7, 14),
        )

    assert exc.value.code == "SEMANTIC_CONTRACT_BLOCKED"
    assert page.read_text(encoding="utf-8") == source


def test_set_frontmatter_project_plan_is_pure_until_semantic_commit(
    tmp_path: Path,
) -> None:
    source = _source("A")
    page = _write(tmp_path, _PAGE, source)
    registry_path = tmp_path / "Knowledge Base/_Schema/project-keys.yaml"
    registry_path.parent.mkdir(parents=True, exist_ok=True)

    preview = set_frontmatter_module.set_frontmatter_field(
        tmp_path,
        path=_PAGE,
        field="project",
        value="new-domain",
        why="preview project move",
        validate_only=True,
        today=dt.date(2026, 7, 14),
    )

    assert preview.validate_only is True
    assert preview.semantic is not None
    assert preview.semantic["mutated"] is False
    assert page.read_text(encoding="utf-8") == source
    assert not registry_path.exists()

    committed = set_frontmatter_module.set_frontmatter_field(
        tmp_path,
        path=_PAGE,
        field="project",
        value="new-domain",
        why="commit project move",
        today=dt.date(2026, 7, 14),
    )
    assert committed.semantic is not None
    assert committed.semantic["mutated"] is True
    assert "project: new-domain" in page.read_text(encoding="utf-8")
    assert "new-domain:" in registry_path.read_text(encoding="utf-8")


def test_set_frontmatter_preliminary_block_does_not_register_project(
    tmp_path: Path,
) -> None:
    source_path = "Knowledge Base/Sources/Articles/source.md"
    _write(tmp_path, source_path, _source("Raw"))
    registry_path = tmp_path / "Knowledge Base/_Schema/project-keys.yaml"

    with pytest.raises(set_frontmatter_module.SetFrontmatterError) as exc:
        set_frontmatter_module.set_frontmatter_field(
            tmp_path,
            path=source_path,
            field="project",
            value="blocked-domain",
            why="must remain inert",
        )

    assert exc.value.code == "APPEND_ONLY"
    assert not registry_path.exists()


def test_set_frontmatter_draft_to_active_commits_exact_reviewed_none(
    tmp_path: Path,
) -> None:
    draft = _source("Draft").replace("status: active", "status: draft")
    page = _write(tmp_path, _PAGE, draft)
    _write(tmp_path, _OTHER_PAGE, _source("Existing", page_id=_OTHER_ID))

    preview = set_frontmatter_module.set_frontmatter_field(
        tmp_path,
        path=_PAGE,
        field="status",
        value="active",
        why="preview activation",
        validate_only=True,
        today=dt.date(2026, 7, 14),
    )
    assert preview.semantic is not None
    assert preview.semantic["applicability"] == "full"
    assert preview.semantic["contract_result"]["should_block"] is True

    reviewed = set_frontmatter_module.set_frontmatter_field(
        tmp_path,
        path=_PAGE,
        field="status",
        value="active",
        why="review and activate",
        validate_only=True,
        today=dt.date(2026, 7, 14),
        semantic_transition_token=preview.semantic["transition_token"],
        relation_disposition="reviewed_none",
        relation_review_hash=preview.semantic["transition_hash"],
        relation_review_reason="No honest relation exists for this activation",
    )
    assert reviewed.semantic is not None
    assert reviewed.semantic["contract_result"]["should_block"] is False

    committed = set_frontmatter_module.set_frontmatter_field(
        tmp_path,
        path=_PAGE,
        field="status",
        value="active",
        why="review and activate",
        today=dt.date(2026, 7, 14),
        semantic_transition_token=reviewed.semantic["transition_token"],
        relation_disposition="reviewed_none",
        relation_review_hash=reviewed.semantic["transition_hash"],
        relation_review_reason="No honest relation exists for this activation",
    )
    assert committed.semantic is not None
    assert committed.semantic["mutated"] is True
    assert committed.semantic["lifecycle_state"] == "new"
    assert "status: active" in page.read_text(encoding="utf-8")
    assert relation_review.lifecycle_prepared_path(tmp_path, _ID).exists()


def test_tier2_overwrite_validate_and_commit_use_existing_coordinator(
    tmp_path: Path,
) -> None:
    source = _source("A")
    page = _write(tmp_path, _PAGE, source)
    after = source.replace("A\n\n## Relations", "B\n\n## Relations")

    preview = create_file_module.create_file(
        tmp_path,
        path=_PAGE,
        content=after,
        overwrite=True,
        validate_only=True,
        today=dt.date(2026, 7, 14),
    )
    assert isinstance(preview, semantic_writes.ExistingPreflight)
    assert preview.operation == "tier2_overwrite"
    assert preview.mutated is False
    assert page.read_text(encoding="utf-8") == source

    committed = create_file_module.create_file(
        tmp_path,
        path=_PAGE,
        content=after,
        overwrite=True,
        draft_token=preview.transition_token,
        today=dt.date(2026, 7, 14),
    )
    assert committed.semantic is not None
    assert committed.semantic["operation"] == "tier2_overwrite"
    assert committed.semantic["mutated"] is True
    assert page.read_text(encoding="utf-8") == after
    assert set(committed.as_dict()) == {"path", "warnings", "semantic"}


def test_tier2_overwrite_true_on_absent_path_preserves_creation_behavior(
    tmp_path: Path,
) -> None:
    rel = "Knowledge Base/Identity/new.md"

    result = create_file_module.create_file(
        tmp_path,
        path=rel,
        content="---\ntype: identity\n---\n# New\n",
        overwrite=True,
        today=dt.date(2026, 7, 14),
    )

    assert result.creation is not None
    assert result.semantic is None
    assert (tmp_path / rel).exists()


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (_source("A"), _source("A", page_id=None)),
        (_source("A"), _source("A", page_id=_OTHER_ID)),
        (
            _source("A"),
            _source("A").replace(
                f"exomem_id: {_ID}\n", f"exomem_id: {_ID}\nexomem_id: {_ID}\n"
            ),
        ),
        (_source("A", page_id=None), _source("A")),
    ],
)
def test_tier2_overwrite_rejects_stable_identity_bypass(
    tmp_path: Path, before: str, after: str
) -> None:
    page = _write(tmp_path, _PAGE, before)

    with pytest.raises(create_file_module.CreateFileError) as exc:
        create_file_module.create_file(
            tmp_path,
            path=_PAGE,
            content=after,
            overwrite=True,
            today=dt.date(2026, 7, 14),
        )

    assert exc.value.code == "STABLE_ID_BYPASS"
    assert page.read_text(encoding="utf-8") == before


def test_tier2_overwrite_detects_concurrent_change_without_clobbering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source("A")
    after = source.replace("A\n\n## Relations", "B\n\n## Relations")
    page = _write(tmp_path, _PAGE, source)
    original = semantic_writes.preflight_existing

    def race(*args, **kwargs):
        page.write_text(source + "\nConcurrent writer.\n", encoding="utf-8")
        return original(*args, **kwargs)

    monkeypatch.setattr(create_file_module.semantic_writes, "preflight_existing", race)

    with pytest.raises(create_file_module.CreateFileError) as exc:
        create_file_module.create_file(
            tmp_path,
            path=_PAGE,
            content=after,
            overwrite=True,
            today=dt.date(2026, 7, 14),
        )

    assert exc.value.code == "STALE_SEMANTIC_WRITE"
    assert page.read_text(encoding="utf-8") == source + "\nConcurrent writer.\n"


def test_existing_coordinator_exact_committed_replay_is_mutation_free(
    tmp_path: Path,
) -> None:
    source = _source("A")
    after = source.replace("A\n\n## Relations", "B\n\n## Relations")
    _write(tmp_path, _PAGE, source)
    preview = semantic_writes.preflight_existing(
        tmp_path,
        path=_PAGE,
        after_source=after,
        operation="tier2_overwrite",
    )
    first = semantic_writes.commit_existing(tmp_path, preflight=preview)
    before_replay = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }

    replay_preview = semantic_writes.preflight_existing(
        tmp_path,
        path=_PAGE,
        after_source=after,
        operation="tier2_overwrite",
        transition_token=first.transition_token,
    )
    replay = semantic_writes.commit_existing(tmp_path, preflight=replay_preview)
    after_replay = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }

    assert replay.mutated is False
    assert replay.lifecycle_state == "committed_replay"
    assert replay.written_paths == ()
    assert after_replay == before_replay


def test_tier2_append_commits_semantics_log_and_primary_in_one_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source("A")
    page = _write(tmp_path, _PAGE, source)
    log_path = tmp_path / "Knowledge Base/log.md"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("# Log\n\n---\n", encoding="utf-8")
    batches: list[list[str]] = []
    original = semantic_writes.vault.batch_atomic_write

    def capture(writes, **kwargs):
        materialized = list(writes)
        batches.append(
            [item.path.relative_to(tmp_path).as_posix() for item in materialized]
        )
        return original(materialized, **kwargs)

    monkeypatch.setattr(semantic_writes.vault, "batch_atomic_write", capture)

    result = append_module.append_to_file(
        tmp_path,
        path=_PAGE,
        content="\nAppended detail.\n",
        today=dt.date(2026, 7, 14),
    )

    assert result.semantic is not None
    assert result.semantic["operation"] == "tier2_append"
    assert result.semantic["mutated"] is True
    assert batches[-1][-1] == _PAGE
    assert batches[-1].count("Knowledge Base/log.md") == 1
    assert page.read_text(encoding="utf-8").endswith("\nAppended detail.\n")


def test_tier2_append_detects_concurrent_change_without_losing_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source("A")
    page = _write(tmp_path, _PAGE, source)
    original = semantic_writes.preflight_existing

    def race(*args, **kwargs):
        page.write_text(source + "\nConcurrent writer.\n", encoding="utf-8")
        return original(*args, **kwargs)

    monkeypatch.setattr(append_module.semantic_writes, "preflight_existing", race)

    with pytest.raises(append_module.AppendError) as exc:
        append_module.append_to_file(
            tmp_path,
            path=_PAGE,
            content="Our append.\n",
            today=dt.date(2026, 7, 14),
        )

    assert exc.value.code == "STALE_SEMANTIC_WRITE"
    assert page.read_text(encoding="utf-8") == source + "\nConcurrent writer.\n"


def test_tier2_append_exact_committed_retry_does_not_duplicate_content(
    tmp_path: Path,
) -> None:
    source = _source("A").rstrip("\n")
    page = _write(tmp_path, _PAGE, source)
    preview = append_module.append_to_file(
        tmp_path,
        path=_PAGE,
        content="Appended once.\n",
        validate_only=True,
        today=dt.date(2026, 7, 14),
    )
    assert isinstance(preview, semantic_writes.ExistingPreflight)
    first = append_module.append_to_file(
        tmp_path,
        path=_PAGE,
        content="Appended once.\n",
        semantic_transition_token=preview.transition_token,
        today=dt.date(2026, 7, 14),
    )
    after = page.read_text(encoding="utf-8")

    replay = append_module.append_to_file(
        tmp_path,
        path=_PAGE,
        content="Appended once.\n",
        semantic_transition_token=first.semantic["transition_token"],
        today=dt.date(2026, 7, 14),
    )

    assert replay.semantic is not None
    assert replay.semantic["mutated"] is False
    assert replay.semantic["lifecycle_state"] == "committed_replay"
    assert page.read_text(encoding="utf-8") == after
    assert after.count("Appended once.") == 1


def test_canonical_decision_and_prepared_round_trip_with_direct_current_lookup(
    tmp_path: Path,
) -> None:
    source_a = _source("A")
    source_b = _source("B")
    page = _write(tmp_path, _PAGE, source_a)
    decision = _decision(source_b)
    prepared = _prepared(
        source_a,
        source_b,
        transition_id=_TRANSITION_AB,
        decision=decision,
    )

    plan = relation_review.plan_lifecycle_transition(
        tmp_path,
        decision=decision,
        prepared=prepared,
        current=_binding(_PAGE, source_a),
    )
    assert plan.state == "new"
    assert [write.path for write in plan.writes] == [
        relation_review.lifecycle_decision_path(
            tmp_path, _ID, decision.after_fingerprint
        ),
        relation_review.lifecycle_prepared_path(tmp_path, _ID),
    ]
    _apply_plan(tmp_path, plan)

    assert relation_review.load_lifecycle_decision(
        tmp_path, _ID, decision.after_fingerprint
    ) == decision
    assert relation_review.load_lifecycle_prepared(tmp_path, _ID) == prepared
    assert page.read_text(encoding="utf-8") == source_a

    corpus_a = semantic_contract.build_corpus_context(tmp_path)
    state_a = corpus_a.pages[_PAGE]
    assert relation_review.load_relation_review(tmp_path, state_a, corpus=corpus_a) is None

    page.write_text(source_b, encoding="utf-8")
    corpus_b = semantic_contract.build_corpus_context(tmp_path)
    state_b = corpus_b.pages[_PAGE]
    current = relation_review.load_relation_review(tmp_path, state_b, corpus=corpus_b)
    assert current is not None
    assert current.kind == "reviewed_none"
    assert current.reference == decision.reference


def test_pending_retry_committed_replay_pending_refusal_and_stale_detection(
    tmp_path: Path,
) -> None:
    source_a = _source("A")
    source_b = _source("B")
    source_c = _source("C")
    decision_b = _decision(source_b)
    prepared_ab = _prepared(
        source_a,
        source_b,
        transition_id=_TRANSITION_AB,
        decision=decision_b,
    )
    initial = relation_review.plan_lifecycle_transition(
        tmp_path,
        decision=decision_b,
        prepared=prepared_ab,
        current=_binding(_PAGE, source_a),
    )
    _apply_plan(tmp_path, initial)

    exact_pending = relation_review.plan_lifecycle_transition(
        tmp_path,
        decision=decision_b,
        prepared=prepared_ab,
        current=_binding(_PAGE, source_a),
    )
    assert exact_pending.state == "pending_retry"
    assert exact_pending.writes == ()

    decision_c = _decision(source_c, reason="C has no honest relation")
    prepared_ac = _prepared(
        source_a,
        source_c,
        transition_id=_TRANSITION_BC,
        decision=decision_c,
    )
    with pytest.raises(relation_review.RelationReviewError) as pending:
        relation_review.plan_lifecycle_transition(
            tmp_path,
            decision=decision_c,
            prepared=prepared_ac,
            current=_binding(_PAGE, source_a),
        )
    assert pending.value.code == "LIFECYCLE_TRANSITION_PENDING"

    with pytest.raises(relation_review.RelationReviewError) as stale:
        relation_review.plan_lifecycle_transition(
            tmp_path,
            decision=decision_b,
            prepared=prepared_ab,
            current=_binding(_PAGE, source_c),
        )
    assert stale.value.code == "LIFECYCLE_TRANSITION_STALE"

    exact_committed = relation_review.plan_lifecycle_transition(
        tmp_path,
        decision=decision_b,
        prepared=prepared_ab,
        current=_binding(_PAGE, source_b),
    )
    assert exact_committed.state == "committed_replay"
    assert exact_committed.writes == ()


def test_committed_slot_replacement_and_exact_revert_reuse_decision(
    tmp_path: Path,
) -> None:
    source_a = _source("A")
    source_b = _source("B")
    source_c = _source("C")
    decision_b = _decision(source_b)
    prepared_ab = _prepared(
        source_a,
        source_b,
        transition_id=_TRANSITION_AB,
        decision=decision_b,
    )
    _apply_plan(
        tmp_path,
        relation_review.plan_lifecycle_transition(
            tmp_path,
            decision=decision_b,
            prepared=prepared_ab,
            current=_binding(_PAGE, source_a),
        ),
    )
    decision_path = relation_review.lifecycle_decision_path(
        tmp_path, _ID, decision_b.after_fingerprint
    )
    decision_bytes = decision_path.read_bytes()

    decision_c = _decision(source_c, reason="C has no honest relation")
    prepared_bc = _prepared(
        source_b,
        source_c,
        transition_id=_TRANSITION_BC,
        decision=decision_c,
    )
    plan_bc = relation_review.plan_lifecycle_transition(
        tmp_path,
        decision=decision_c,
        prepared=prepared_bc,
        current=_binding(_PAGE, source_b),
    )
    assert plan_bc.state == "replace_committed"
    _apply_plan(tmp_path, plan_bc)

    prepared_cb = _prepared(
        source_c,
        source_b,
        transition_id=_TRANSITION_CB,
        decision=decision_b,
    )
    plan_cb = relation_review.plan_lifecycle_transition(
        tmp_path,
        decision=decision_b,
        prepared=prepared_cb,
        current=_binding(_PAGE, source_c),
    )
    assert plan_cb.state == "replace_committed"
    _apply_plan(tmp_path, plan_cb)

    assert decision_path.read_bytes() == decision_bytes
    assert relation_review.load_lifecycle_prepared(tmp_path, _ID) == prepared_cb
    assert len({_TRANSITION_AB, _TRANSITION_BC, _TRANSITION_CB}) == 3


def test_decision_reason_and_pending_token_drift_fail_before_mutation(
    tmp_path: Path,
) -> None:
    source_a = _source("A")
    source_b = _source("B")
    decision_b = _decision(source_b)
    prepared_ab = _prepared(
        source_a,
        source_b,
        transition_id=_TRANSITION_AB,
        decision=decision_b,
    )
    _apply_plan(
        tmp_path,
        relation_review.plan_lifecycle_transition(
            tmp_path,
            decision=decision_b,
            prepared=prepared_ab,
            current=_binding(_PAGE, source_a),
        ),
    )
    before = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*.json")
    }

    changed_reason = _decision(source_b, reason="A different judgment")
    with pytest.raises(relation_review.RelationReviewError) as collision:
        relation_review.plan_lifecycle_transition(
            tmp_path,
            decision=changed_reason,
            prepared=_prepared(
                source_a,
                source_b,
                transition_id=_TRANSITION_AB,
                decision=changed_reason,
            ),
            current=_binding(_PAGE, source_a),
        )
    assert collision.value.code == "LIFECYCLE_TRANSITION_MISMATCH"

    with pytest.raises(relation_review.RelationReviewError) as pending:
        relation_review.plan_lifecycle_transition(
            tmp_path,
            decision=changed_reason,
            prepared=_prepared(
                source_a,
                source_b,
                transition_id=_TRANSITION_BC,
                decision=changed_reason,
            ),
            current=_binding(_PAGE, source_a),
        )
    assert pending.value.code == "LIFECYCLE_TRANSITION_PENDING"

    token_drift = _prepared(
        source_a,
        source_b,
        transition_id=_TRANSITION_AB,
        decision=decision_b,
        token="changed-token",
    )
    with pytest.raises(relation_review.RelationReviewError) as mismatch:
        relation_review.plan_lifecycle_transition(
            tmp_path,
            decision=decision_b,
            prepared=token_drift,
            current=_binding(_PAGE, source_a),
        )
    assert mismatch.value.code == "LIFECYCLE_TRANSITION_MISMATCH"
    assert {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*.json")
    } == before


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda value: {**value, "extra": True}, "RELATION_REVIEW_INVALID_SCHEMA"),
        (
            lambda value: {key: item for key, item in value.items() if key != "reason"},
            "RELATION_REVIEW_INVALID_SCHEMA",
        ),
        (
            lambda value: {**value, "schema_version": True},
            "RELATION_REVIEW_INVALID_SCHEMA",
        ),
        (
            lambda value: {**value, "schema_version": 99},
            "RELATION_REVIEW_UNSUPPORTED_VERSION",
        ),
        (
            lambda value: {**value, "decision_hash": "A" * 64},
            "RELATION_REVIEW_INVALID_SCHEMA",
        ),
        (lambda value: {**value, "reason": "  padded  "}, "RELATION_REVIEW_INVALID_SCHEMA"),
    ],
)
def test_lifecycle_decision_schema_is_strict(
    tmp_path: Path, mutate, code: str
) -> None:
    decision = _decision(_source("B"))
    path = relation_review.lifecycle_decision_path(
        tmp_path, _ID, decision.after_fingerprint
    )
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(mutate(decision.storage_dict())), encoding="utf-8")

    with pytest.raises(relation_review.RelationReviewError) as exc:
        relation_review.load_lifecycle_decision(
            tmp_path, _ID, decision.after_fingerprint
        )
    assert exc.value.code == code


def test_lifecycle_bounds_alias_symlink_and_history_limit_are_closed(
    tmp_path: Path,
) -> None:
    decision = _decision(_source("B"))
    path = relation_review.lifecycle_decision_path(
        tmp_path, _ID, decision.after_fingerprint
    )
    path.parent.mkdir(parents=True)
    path.write_bytes(b"{" + b"x" * (16 * 1024))
    with pytest.raises(relation_review.RelationReviewError) as too_large:
        relation_review.load_lifecycle_decision(
            tmp_path, _ID, decision.after_fingerprint
        )
    assert too_large.value.code == "RELATION_REVIEW_TOO_LARGE"

    path.unlink()
    alias = path.with_name(f"{decision.after_fingerprint.upper()}.JSON")
    alias.write_text("{}", encoding="utf-8")
    with pytest.raises(relation_review.RelationReviewError) as alias_error:
        relation_review.load_lifecycle_decision(
            tmp_path, _ID, decision.after_fingerprint
        )
    assert alias_error.value.code == "RELATION_REVIEW_ALIAS"

    alias.unlink()
    target = tmp_path / "outside.json"
    target.write_text("{}", encoding="utf-8")
    try:
        path.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    with pytest.raises(relation_review.RelationReviewError) as unsafe:
        relation_review.load_lifecycle_decision(
            tmp_path, _ID, decision.after_fingerprint
        )
    assert unsafe.value.code == "RELATION_REVIEW_UNSAFE_FILE"
    path.unlink()

    for index in range(257):
        fingerprint = f"{index:064x}"
        item = relation_review.build_lifecycle_decision(
            page_identity=_ID,
            after_fingerprint=fingerprint,
            reason="Reviewed",
        )
        item_path = relation_review.lifecycle_decision_path(
            tmp_path, _ID, fingerprint
        )
        item_path.write_text(
            relation_review.serialize_lifecycle_decision(item), encoding="utf-8"
        )
    with pytest.raises(relation_review.RelationReviewError) as overflow:
        relation_review.load_lifecycle_decision(tmp_path, _ID, f"{0:064x}")
    assert overflow.value.code == "RELATION_REVIEW_HISTORY_LIMIT"


def test_lifecycle_residue_policy_accepts_only_three_atomic_batch_files(
    tmp_path: Path,
) -> None:
    decision = _decision(_source("B"))
    path = relation_review.lifecycle_decision_path(
        tmp_path, _ID, decision.after_fingerprint
    )
    path.parent.mkdir(parents=True)
    path.write_text(
        relation_review.serialize_lifecycle_decision(decision), encoding="utf-8"
    )
    supported = (
        f".{path.name}.abcdefgh.tmp",
        ".prepared.json.abcdefgh.tmp",
        ".prepared.json.abcdefgh.bak",
    )
    for name in supported:
        (path.parent / name).write_text("atomic residue", encoding="utf-8")

    assert (
        relation_review.load_lifecycle_decision(
            tmp_path, _ID, decision.after_fingerprint
        )
        == decision
    )

    (path.parent / f".{path.name}.ijklmnop.bak").write_text(
        "excess residue", encoding="utf-8"
    )
    with pytest.raises(relation_review.RelationReviewError) as excessive:
        relation_review.load_lifecycle_decision(
            tmp_path, _ID, decision.after_fingerprint
        )
    assert excessive.value.code == "RELATION_REVIEW_DIRECTORY_LIMIT"


def test_lifecycle_arbitrary_tmp_suffix_is_not_ignored(tmp_path: Path) -> None:
    directory = relation_review.lifecycle_prepared_path(tmp_path, _ID).parent
    directory.mkdir(parents=True)
    (directory / "arbitrary.tmp").write_text("not batch residue", encoding="utf-8")

    with pytest.raises(relation_review.RelationReviewError) as unsafe:
        relation_review.load_lifecycle_prepared(tmp_path, _ID)

    assert unsafe.value.code == "RELATION_REVIEW_ALIAS"


def test_planning_a_257th_lifecycle_decision_fails_closed(tmp_path: Path) -> None:
    directory = relation_review.lifecycle_prepared_path(tmp_path, _ID).parent
    directory.mkdir(parents=True)
    for index in range(256):
        fingerprint = f"{index:064x}"
        decision = relation_review.build_lifecycle_decision(
            page_identity=_ID,
            after_fingerprint=fingerprint,
            reason="Reviewed",
        )
        relation_review.lifecycle_decision_path(
            tmp_path, _ID, fingerprint
        ).write_text(
            relation_review.serialize_lifecycle_decision(decision), encoding="utf-8"
        )

    source_a = _source("A")
    source_b = _source("B")
    decision_b = _decision(source_b)
    prepared_ab = _prepared(
        source_a,
        source_b,
        transition_id=_TRANSITION_AB,
        decision=decision_b,
    )
    with pytest.raises(relation_review.RelationReviewError) as overflow:
        relation_review.plan_lifecycle_transition(
            tmp_path,
            decision=decision_b,
            prepared=prepared_ab,
            current=_binding(_PAGE, source_a),
        )
    assert overflow.value.code == "RELATION_REVIEW_HISTORY_LIMIT"


def test_guarded_apply_refuses_a_concurrent_257th_lifecycle_decision(
    tmp_path: Path,
) -> None:
    directory = relation_review.lifecycle_prepared_path(tmp_path, _ID).parent
    directory.mkdir(parents=True)
    for index in range(255):
        fingerprint = f"{index:064x}"
        decision = relation_review.build_lifecycle_decision(
            page_identity=_ID,
            after_fingerprint=fingerprint,
            reason="Reviewed",
        )
        relation_review.lifecycle_decision_path(
            tmp_path, _ID, fingerprint
        ).write_text(
            relation_review.serialize_lifecycle_decision(decision), encoding="utf-8"
        )

    source_a = _source("A")
    source_b = _source("B")
    primary = _write(tmp_path, _PAGE, source_a)
    planned_decision = _decision(source_b)
    prepared = _prepared(
        source_a,
        source_b,
        transition_id=_TRANSITION_AB,
        decision=planned_decision,
    )
    plan = relation_review.plan_lifecycle_transition(
        tmp_path,
        decision=planned_decision,
        prepared=prepared,
        current=_binding(_PAGE, source_a),
    )

    concurrent_fingerprint = f"{255:064x}"
    concurrent_decision = relation_review.build_lifecycle_decision(
        page_identity=_ID,
        after_fingerprint=concurrent_fingerprint,
        reason="Concurrent review",
    )
    relation_review.lifecycle_decision_path(
        tmp_path, _ID, concurrent_fingerprint
    ).write_text(
        relation_review.serialize_lifecycle_decision(concurrent_decision),
        encoding="utf-8",
    )

    with pytest.raises(vault.PathGuardError) as changed:
        _apply_plan(tmp_path, plan)

    assert changed.value.code == "PATH_GUARD_CHANGED"
    assert not relation_review.lifecycle_decision_path(
        tmp_path, _ID, planned_decision.after_fingerprint
    ).exists()
    assert not relation_review.lifecycle_prepared_path(tmp_path, _ID).exists()
    assert primary.read_text(encoding="utf-8") == source_a
    assert len(tuple(directory.glob("[0-9a-f]" * 64 + ".json"))) == 256


def test_lifecycle_identity_directory_swap_is_detected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    decision = _decision(_source("B"))
    artifact = relation_review.lifecycle_decision_path(
        tmp_path, _ID, decision.after_fingerprint
    )
    artifact.parent.mkdir(parents=True)
    artifact.write_text(
        relation_review.serialize_lifecycle_decision(decision), encoding="utf-8"
    )
    directory = artifact.parent
    displaced = directory.with_name(f"{directory.name}-displaced")
    real_open = os.open
    swapped = False

    def swap_before_artifact_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if not swapped and Path(path).name == artifact.name:
            swapped = True
            directory.rename(displaced)
            directory.mkdir()
            os.link(displaced / artifact.name, directory / artifact.name)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(relation_review.os, "open", swap_before_artifact_open)

    with pytest.raises(relation_review.RelationReviewError) as exc:
        relation_review.load_lifecycle_decision(
            tmp_path, _ID, decision.after_fingerprint
        )
    assert exc.value.code == "RELATION_REVIEW_SWAPPED"


def test_duplicate_key_prepared_alias_and_unsafe_identity_directory_are_rejected(
    tmp_path: Path,
) -> None:
    prepared_path = relation_review.lifecycle_prepared_path(tmp_path, _ID)
    prepared_path.parent.mkdir(parents=True)
    prepared_path.write_text(
        '{"schema_version":1,"schema_version":1}', encoding="utf-8"
    )
    with pytest.raises(relation_review.RelationReviewError) as duplicate:
        relation_review.load_lifecycle_prepared(tmp_path, _ID)
    assert duplicate.value.code == "RELATION_REVIEW_DUPLICATE_KEY"

    prepared_path.unlink()
    prepared_path.with_name("PREPARED.JSON").write_text("{}", encoding="utf-8")
    with pytest.raises(relation_review.RelationReviewError) as alias:
        relation_review.load_lifecycle_prepared(tmp_path, _ID)
    assert alias.value.code == "RELATION_REVIEW_ALIAS"

    identity_dir = prepared_path.parent
    for child in identity_dir.iterdir():
        child.unlink()
    identity_dir.rmdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        identity_dir.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks unavailable")
    with pytest.raises(relation_review.RelationReviewError) as unsafe:
        relation_review.load_lifecycle_prepared(tmp_path, _ID)
    assert unsafe.value.code == "RELATION_REVIEW_DIRECTORY_UNSAFE"


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda value: {**value, "extra": True}, "RELATION_REVIEW_INVALID_SCHEMA"),
        (
            lambda value: {
                key: item for key, item in value.items() if key != "carried_from"
            },
            "RELATION_REVIEW_INVALID_SCHEMA",
        ),
        (
            lambda value: {**value, "schema_version": True},
            "RELATION_REVIEW_INVALID_SCHEMA",
        ),
        (
            lambda value: {**value, "contract_version": 99},
            "RELATION_REVIEW_UNSUPPORTED_VERSION",
        ),
        (
            lambda value: {
                **value,
                "decision_reference": None,
                "decision_bytes_hash": "a" * 64,
            },
            "RELATION_REVIEW_INVALID_SCHEMA",
        ),
        (
            lambda value: {**value, "transition_id": _TRANSITION_AB.upper()},
            "RELATION_REVIEW_INVALID_SCHEMA",
        ),
    ],
)
def test_lifecycle_prepared_schema_is_strict(
    tmp_path: Path, mutate, code: str
) -> None:
    source_a = _source("A")
    source_b = _source("B")
    decision = _decision(source_b)
    prepared = _prepared(
        source_a,
        source_b,
        transition_id=_TRANSITION_AB,
        decision=decision,
    )
    path = relation_review.lifecycle_prepared_path(tmp_path, _ID)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(mutate(prepared.storage_dict())), encoding="utf-8")

    with pytest.raises(relation_review.RelationReviewError) as invalid:
        relation_review.load_lifecycle_prepared(tmp_path, _ID)

    assert invalid.value.code == code


def test_lifecycle_loader_prefers_decision_then_current_creation_receipt_and_never_legacy(
    tmp_path: Path,
) -> None:
    source = _source("Reviewed")
    _write(tmp_path, _PAGE, source)
    decision = _decision(source, reason="Lifecycle decision wins")
    decision_path = relation_review.lifecycle_decision_path(
        tmp_path, _ID, decision.after_fingerprint
    )
    decision_path.parent.mkdir(parents=True)
    decision_path.write_text(
        relation_review.serialize_lifecycle_decision(decision), encoding="utf-8"
    )
    legacy_receipt = relation_review.review_artifact_path(tmp_path, _ID)
    legacy_receipt.parent.mkdir(parents=True, exist_ok=True)
    legacy_receipt.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "reviewed_none",
                "page_identity": _ID,
                "page_path_at_review": _PAGE,
                "content_fingerprint": decision.after_fingerprint,
                "draft_hash": "a" * 64,
                "auxiliary_hash": "b" * 64,
                "reason": "Creation decision",
            }
        ),
        encoding="utf-8",
    )
    corpus = semantic_contract.build_corpus_context(tmp_path)
    page = corpus.pages[_PAGE]
    loaded = relation_review.load_relation_review(tmp_path, page, corpus=corpus)
    assert loaded is not None
    assert loaded.reference == decision.reference
    assert loaded.reason == "Lifecycle decision wins"

    legacy_source = _source("Legacy", page_id=None)
    legacy_path = _write(
        tmp_path,
        "Knowledge Base/Notes/Insights/legacy.md",
        legacy_source,
    )
    legacy_corpus = semantic_contract.build_corpus_context(tmp_path)
    legacy_state = legacy_corpus.pages[legacy_path.relative_to(tmp_path).as_posix()]
    assert legacy_state.identity_kind == "path"
    assert (
        relation_review.load_relation_review(
            tmp_path, legacy_state, corpus=legacy_corpus
        )
        is None
    )
    with pytest.raises(relation_review.RelationReviewError) as invalid:
        relation_review.build_lifecycle_decision(
            page_identity=legacy_state.identity,
            after_fingerprint="c" * 64,
            reason="must backfill first",
        )
    assert invalid.value.code == "RELATION_REVIEW_INVALID_ID"


@pytest.mark.parametrize("malformed", [False, True])
def test_lifecycle_state_reserves_uuid_against_new_6d_creation(
    tmp_path: Path, malformed: bool
) -> None:
    source = _source("New creation")
    fingerprint = semantic_contract.review_content_fingerprint(_ID, source)
    if malformed:
        path = relation_review.lifecycle_prepared_path(tmp_path, _ID)
        path.parent.mkdir(parents=True)
        path.write_text("not-json", encoding="utf-8")
    else:
        decision = relation_review.build_lifecycle_decision(
            page_identity=_ID,
            after_fingerprint=fingerprint,
            reason="Reserved historical identity",
        )
        path = relation_review.lifecycle_decision_path(tmp_path, _ID, fingerprint)
        path.parent.mkdir(parents=True)
        path.write_text(
            relation_review.serialize_lifecycle_decision(decision), encoding="utf-8"
        )

    with pytest.raises(relation_review.RelationReviewError) as exc:
        relation_review.validate_creation_draft(
            tmp_path,
            path=_PAGE,
            source=source,
            draft_id=_ID,
            operation="create",
        )
    assert exc.value.code == "DRAFT_ID_IN_USE"
    assert not (tmp_path / _PAGE).exists()


def test_malformed_lifecycle_state_does_not_break_exact_existing_6d_replay(
    tmp_path: Path,
) -> None:
    source = _source("Committed creation")
    committed = relation_review.commit_creation_draft(
        tmp_path,
        path=_PAGE,
        source=source,
        draft_id=_ID,
        operation="create",
    )
    malformed = relation_review.lifecycle_prepared_path(tmp_path, _ID)
    malformed.parent.mkdir(parents=True)
    malformed.write_text("not-json", encoding="utf-8")

    with pytest.raises(relation_review.RelationReviewError) as replay:
        relation_review.commit_creation_draft(
            tmp_path,
            path=_PAGE,
            source=source,
            draft_id=_ID,
            operation="create",
        )
    assert committed.relation_disposition == "bootstrap"
    assert replay.value.code == "DRAFT_ALREADY_COMMITTED"


def test_qualifying_v2_receipt_is_recovery_only_not_review_truth(tmp_path: Path) -> None:
    source = _source("Qualifying")
    _write(tmp_path, _PAGE, source)
    artifact = relation_review.review_artifact_path(tmp_path, _ID)
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "kind": "qualifying",
                "page_identity": _ID,
                "page_path_at_review": _PAGE,
                "content_fingerprint": semantic_contract.review_content_fingerprint(
                    _ID, source
                ),
                "draft_hash": "a" * 64,
                "auxiliary_hash": "b" * 64,
                "reason": None,
                "operation": "create",
                "draft_token_hash": hashlib.sha256(b"").hexdigest(),
                "predecessor_path": None,
                "predecessor_content_hash": None,
            }
        ),
        encoding="utf-8",
    )
    corpus = semantic_contract.build_corpus_context(tmp_path)
    assert (
        relation_review.load_relation_review(
            tmp_path, corpus.pages[_PAGE], corpus=corpus
        )
        is None
    )


def test_pure_activation_snapshot_and_boundary_plan_write_nothing_and_do_not_mutate(
    tmp_path: Path,
) -> None:
    candidates = (
        activation_manifest.ActivationCandidate(
            _PAGE, "a" * 64, _ID
        ),
        activation_manifest.ActivationCandidate(
            _OTHER_PAGE, "b" * 64, _OTHER_ID
        ),
    )
    census = activation_manifest.ActivationCensus.from_candidates(candidates)
    before_candidates = census.candidates
    before_files = tuple(tmp_path.rglob("*"))

    snapshot = activation_manifest.snapshot_from_census(census)
    prospective = activation_manifest.plan_activation_boundary(census, manifest=None)
    observed = activation_manifest.plan_activation_boundary(
        census, manifest=snapshot
    )

    assert snapshot == activation_manifest._snapshot(tmp_path, census=census)
    assert prospective.manifest == snapshot
    assert prospective.install_required is True
    assert observed.manifest == snapshot
    assert observed.install_required is False
    assert census.candidates == before_candidates
    assert tuple(tmp_path.rglob("*")) == before_files


def test_prepared_currentness_is_pure_and_never_treats_pending_as_review() -> None:
    source_a = _source("A")
    source_b = _source("B")
    decision_b = _decision(source_b)
    prepared = _prepared(
        source_a,
        source_b,
        transition_id=_TRANSITION_AB,
        decision=decision_b,
    )
    pending = _binding(_PAGE, source_a)
    committed = _binding(_PAGE, source_b)
    stale = replace(pending, source_hash="f" * 64)

    assert relation_review.lifecycle_prepared_state(prepared, pending) == "pending"
    assert relation_review.lifecycle_prepared_state(prepared, committed) == "committed"
    assert relation_review.lifecycle_prepared_state(prepared, stale) == "stale"
    assert not semantic_contract.is_relation_review_current(
        semantic_contract.RelationReviewState(
            "reviewed_none",
            _ID,
            decision_b.after_fingerprint,
            reason=decision_b.reason,
        ),
        semantic_contract.build_page_state(Path("."), _PAGE, source_a),
        semantic_contract.SemanticCorpusContext.from_states(
            Path("."),
            (semantic_contract.build_page_state(Path("."), _PAGE, source_a),),
            registry=semantic_contract.relation_registry.core_registry(),
            identity_census=semantic_contract.StableIdentityCensus(
                (semantic_contract.StableIdentityEntry(_PAGE, _ID),)
            ),
        ),
    )
