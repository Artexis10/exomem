from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
from pathlib import Path

import pytest

from exomem import (
    create_file,
    find,
    indexes,
    link,
    note,
    project_keys,
    relation_review,
    replace,
    semantic_writes,
)
from exomem import (
    vault as vault_module,
)

TODAY = dt.date(2026, 7, 14)


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def test_writer_resolver_snapshot_forks_without_mutating_shared_cache(vault: Path) -> None:
    shared = find.shared_resolver(vault)
    before = (
        set(shared.full_paths),
        set(shared.kb_stripped),
        {key: list(value) for key, value in shared.stems.items()},
        {key: list(value) for key, value in shared.titles.items()},
    )

    detached = find.writer_resolver_snapshot(vault)
    detached.add_pending("Knowledge Base/Notes/Insights/pending", title="Pending")

    assert detached is not shared
    assert "Knowledge Base/Notes/Insights/pending" in detached.full_paths
    assert (
        shared.full_paths,
        shared.kb_stripped,
        shared.stems,
        shared.titles,
    ) == before


def test_project_key_plan_is_deterministic_folded_and_mutation_free(vault: Path) -> None:
    before = _tree_bytes(vault)

    first = project_keys.plan_project_keys(
        vault,
        ["vehicles", "automotive-something", "vehicles"],
        category="domain",
    )
    second = project_keys.plan_project_keys(
        vault,
        ["vehicles", "automotive-something", "vehicles"],
        category="domain",
    )

    assert first == second
    assert first.introduced_keys == ("vehicles", "automotive-something")
    assert first.registry.folder_for("vehicles") == "Vehicles"
    assert first.registry.folder_for("automotive-something") == "Automotive Something"
    assert len(first.writes) == 1
    assert first.writes[0].path.name == "project-keys.yaml"
    assert _tree_bytes(vault) == before


def test_project_key_replay_includes_exact_already_applied_registry(vault: Path) -> None:
    first = project_keys.plan_project_keys(
        vault, ["semantic-replay-project"], category="domain"
    )
    assert len(first.writes) == 1
    vault_module_write = first.writes[0]
    vault_module.batch_atomic_write(first.writes, vault_root=vault)

    replay = project_keys.plan_project_keys(
        vault,
        ["semantic-replay-project"],
        category="domain",
        replay_introductions=first.introductions,
    )

    assert len(replay.writes) == 1
    assert replay.writes[0].path == vault_module_write.path
    assert replay.writes[0].content == vault_module_write.content


def test_concurrent_project_registry_update_is_not_overwritten(vault: Path) -> None:
    kwargs = {
        "content": "# Guarded registry\n\nA reviewed project conclusion.\n",
        "note_type": "research-note",
        "title": "Guarded project registration",
        "project": "guarded-project-key",
        "project_category": "domain",
        "today": TODAY,
    }
    validation = note.note(vault, validate_only=True, **kwargs)
    prepared = note.note(
        vault,
        draft_id=validation.draft_id,
        draft_hash=validation.draft_hash,
        draft_token=validation.draft_token,
        _return_prepared=True,
        **kwargs,
    )
    registry_path = vault / "Knowledge Base" / "_Schema" / "project-keys.yaml"
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(
        "projects:\n"
        "  concurrent-project:\n"
        "    folder: Concurrent Project\n"
        "    category: domain\n",
        encoding="utf-8",
    )

    with pytest.raises(relation_review.RelationReviewError) as exc:
        semantic_writes.commit_creation(
            vault,
            preflight=prepared.preflight,
            auxiliary_writes=prepared.auxiliary_writes,
            relation_disposition="reviewed_none",
            relation_review_hash=validation.draft_hash,
            relation_review_reason="No honest relation exists in the fixture corpus.",
            operation="create",
        )

    assert exc.value.code in {"PATH_GUARD_CHANGED", "PATH_GUARD_CONTENT"}
    assert "concurrent-project" in registry_path.read_text(encoding="utf-8")
    assert not (vault / validation.destination).exists()


@pytest.mark.parametrize(
    "target_name",
    ("source_backref", "top_index", "sources_index", "log"),
)
def test_prepared_note_rejects_auxiliary_drift_from_exact_read_snapshot(
    vault: Path, target_name: str
) -> None:
    source_rel = "Knowledge Base/Sources/Articles/2026-06-02-postgres-autovacuum-tuning.md"
    kwargs = {
        "content": "# Guard every auxiliary\n\nA conclusion with governed evidence.\n",
        "note_type": "research-note",
        "title": f"Guard every auxiliary {target_name}",
        "project": "guard-matrix-project",
        "project_category": "domain",
        "sources": [source_rel],
        "today": TODAY,
    }
    validation = note.note(vault, validate_only=True, **kwargs)
    prepared = note.note(
        vault,
        draft_id=validation.draft_id,
        draft_hash=validation.draft_hash,
        draft_token=validation.draft_token,
        _return_prepared=True,
        **kwargs,
    )
    targets = {
        "source_backref": vault / source_rel,
        "top_index": vault / "Knowledge Base" / "index.md",
        "sources_index": vault / "Knowledge Base" / "Sources" / "index.md",
        "log": vault / "Knowledge Base" / "log.md",
    }
    target = targets[target_name]
    marker = f"Concurrent edit for {target_name}."
    target.write_text(
        target.read_text(encoding="utf-8") + f"\n{marker}\n", encoding="utf-8"
    )

    with pytest.raises(relation_review.RelationReviewError) as exc:
        semantic_writes.commit_creation(
            vault,
            preflight=prepared.preflight,
            auxiliary_writes=prepared.auxiliary_writes,
            relation_disposition="reviewed_none",
            relation_review_hash=validation.draft_hash,
            relation_review_reason="No qualifying relation exists in this fixture corpus.",
            operation="create",
        )

    assert exc.value.code in {"PATH_GUARD_CHANGED", "PATH_GUARD_CONTENT"}
    assert marker in target.read_text(encoding="utf-8")
    assert not (vault / validation.destination).exists()


def test_project_registry_plan_uses_one_guarded_read_snapshot(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry_path = vault / "Knowledge Base" / "_Schema" / "project-keys.yaml"
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(
        "projects:\n"
        "  existing-project:\n"
        "    folder: Existing Project\n"
        "    category: domain\n",
        encoding="utf-8",
    )
    real_read_bytes = Path.read_bytes
    injected = False

    def inject_before_bytes(path: Path) -> bytes:
        nonlocal injected
        if path == registry_path and not injected:
            injected = True
            path.write_text(
                path.read_text(encoding="utf-8") + "# concurrent registry marker\n",
                encoding="utf-8",
            )
        return real_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", inject_before_bytes)
    plan = project_keys.plan_project_keys(
        vault, ["new-guarded-project"], category="domain"
    )
    vault_module.batch_atomic_write(plan.writes, vault_root=vault)

    assert "# concurrent registry marker" in registry_path.read_text(encoding="utf-8")


def test_log_rotation_rejects_concurrent_creation_of_absent_archive(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXOMEM_LOG_ROTATE_BYTES", "1")
    log_path = vault / "Knowledge Base" / "log.md"
    entries = "".join(
        f"## [2026-07-14] note | item-{index}\n\nbody\n\n"
        for index in range(vault_module.LOG_ROTATE_KEEP_ENTRIES + 5)
    )
    log_path.write_text(f"# Log\n\n---\n{entries}", encoding="utf-8")
    plan = vault_module.plan_log_writes(
        vault,
        date_iso=TODAY.isoformat(),
        op="note",
        rel_path_no_ext="Knowledge Base/Notes/Insights/archive-race",
        body="Guard the absent archive.",
        operation_token="archive-race-token",
    )
    archive_write = next(
        write for write in plan.writes if write.path.parent.name == "logs"
    )
    archive_write.path.parent.mkdir(parents=True, exist_ok=True)
    archive_write.path.write_text("concurrent archive\n", encoding="utf-8")

    with pytest.raises(vault_module.PathGuardError):
        vault_module.batch_atomic_write(plan.writes, vault_root=vault)

    assert archive_write.path.read_text(encoding="utf-8") == "concurrent archive\n"


def test_subindex_planner_can_include_unchanged_targets(vault: Path) -> None:
    changed_only, _ = indexes.compute_subindex_writes(vault)
    replay_stable, _ = indexes.compute_subindex_writes(vault, include_unchanged=True)

    assert len(replay_stable) >= len(changed_only)
    assert {write.path for write in replay_stable} >= {
        path
        for path in (
            vault / "Knowledge Base" / "Sources" / "index.md",
            vault / "Knowledge Base" / "Notes" / "index.md",
            vault / "Knowledge Base" / "Entities" / "index.md",
        )
        if path.exists()
    }


def test_note_validate_only_freezes_path_and_date_without_any_mutation(vault: Path) -> None:
    before = _tree_bytes(vault)

    validation = note.note(
        vault,
        content="# Delayed note\n\n## Decision\n\nKeep the validated destination.\n",
        note_type="research-note",
        title="Delayed note",
        project="vehicles",
        today=TODAY,
        validate_only=True,
    )

    assert validation.mutated is False
    assert validation.draft_token
    assert validation.destination == (
        "Knowledge Base/Notes/Research/Vehicles/delayed-note.md"
    )
    assert _tree_bytes(vault) == before
    assert not (vault / "Knowledge Base/Notes/Research/Vehicles").exists()


def test_note_validate_across_midnight_commits_frozen_date_and_destination(
    vault: Path,
) -> None:
    kwargs = {
        "content": "# Frozen note\n\nA disconnected but reviewed conclusion.\n",
        "note_type": "insight",
        "title": "Frozen midnight destination",
    }
    validation = note.note(vault, today=TODAY, validate_only=True, **kwargs)

    result = note.note(
        vault,
        today=TODAY + dt.timedelta(days=1),
        draft_id=validation.draft_id,
        draft_hash=validation.draft_hash,
        draft_token=validation.draft_token,
        relation_disposition="reviewed_none",
        relation_review_hash=validation.draft_hash,
        relation_review_reason="No honest relation exists in the fixture corpus.",
        **kwargs,
    )

    assert result.path == validation.destination
    text = (vault / result.path).read_text(encoding="utf-8")
    assert f"created: {TODAY.isoformat()}" in text
    receipt = relation_review.load_creation_receipt(vault, validation.draft_id)
    assert receipt is not None and receipt.kind == "reviewed_none"


def test_note_frozen_destination_occupation_fails_without_reselection(vault: Path) -> None:
    kwargs = {
        "content": "# Occupied note\n\nA reviewed conclusion.\n",
        "note_type": "insight",
        "title": "Frozen occupied destination",
    }
    validation = note.note(vault, today=TODAY, validate_only=True, **kwargs)
    occupied = vault / validation.destination
    occupied.parent.mkdir(parents=True, exist_ok=True)
    occupied.write_text("unrelated occupant\n", encoding="utf-8")

    with pytest.raises(note.NoteError) as exc:
        note.note(
            vault,
            today=TODAY + dt.timedelta(days=1),
            draft_id=validation.draft_id,
            draft_hash=validation.draft_hash,
            draft_token=validation.draft_token,
            relation_disposition="reviewed_none",
            relation_review_hash=validation.draft_hash,
            relation_review_reason="No honest relation exists in the fixture corpus.",
            **kwargs,
        )

    assert exc.value.code == "DRAFT_DESTINATION_OCCUPIED"
    assert occupied.read_text(encoding="utf-8") == "unrelated occupant\n"


def test_cold_note_validation_does_not_warm_resolver_cache(vault: Path) -> None:
    find.clear_cache()
    assert find.cache_status()["resolvers"]["entries"] == 0

    note.note(
        vault,
        content="# Cold validation\n\nNo mutation.\n",
        note_type="insight",
        title="Cold resolver validation",
        today=TODAY,
        validate_only=True,
    )

    assert find.cache_status()["resolvers"]["entries"] == 0


def test_note_partial_commit_after_registry_replacement_recovers_exactly(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class SimulatedProcessDeath(BaseException):
        pass

    kwargs = {
        "content": "# Recoverable note\n\nA reviewed project conclusion.\n",
        "note_type": "research-note",
        "title": "Recoverable semantic note",
        "project": "semantic-recovery-project",
        "project_category": "domain",
        "today": TODAY,
    }
    validation = note.note(vault, validate_only=True, **kwargs)
    commit_kwargs = {
        "draft_id": validation.draft_id,
        "draft_hash": validation.draft_hash,
        "draft_token": validation.draft_token,
        "relation_disposition": "reviewed_none",
        "relation_review_hash": validation.draft_hash,
        "relation_review_reason": "No honest relation exists in the fixture corpus.",
    }
    real_batch = relation_review.vault.batch_atomic_write
    real_replace = vault_module.os.replace
    replacements = 0
    captured: list[tuple[tuple[str, str], ...]] = []

    def capture_batch(writes: object, **batch_kwargs: object):
        detached = tuple(writes)  # type: ignore[arg-type]
        captured.append(
            tuple(
                (
                    write.path.relative_to(vault).as_posix(),
                    hashlib.sha256(write.content.encode("utf-8")).hexdigest(),
                )
                for write in detached
            )
        )
        return real_batch(detached, **batch_kwargs)

    def die_after_registry(src: object, dst: object) -> None:
        nonlocal replacements
        real_replace(src, dst)
        destination = Path(dst)  # type: ignore[arg-type]
        if str(src).endswith(".tmp") and destination.is_relative_to(vault):
            replacements += 1
            if replacements == 2:  # receipt, then project registry
                raise SimulatedProcessDeath

    monkeypatch.setattr(relation_review.vault, "batch_atomic_write", capture_batch)
    monkeypatch.setattr(vault_module.os, "replace", die_after_registry)
    with pytest.raises(SimulatedProcessDeath):
        note.note(vault, **commit_kwargs, **kwargs)
    monkeypatch.setattr(vault_module.os, "replace", real_replace)

    assert not (vault / validation.destination).exists()
    receipt_path = relation_review.review_artifact_path(vault, validation.draft_id)
    before_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    result = note.note(vault, **commit_kwargs, **kwargs)
    after_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    artifact_rel = receipt_path.relative_to(vault).as_posix()
    first_auxiliaries = tuple(
        item for item in captured[0][:-1] if item[0] != artifact_rel
    )
    second_auxiliaries = captured[1][:-1]

    assert result.path == validation.destination
    assert first_auxiliaries == second_auxiliaries
    assert before_receipt["auxiliary_hash"] == after_receipt["auxiliary_hash"]
    for relative, expected_hash in second_auxiliaries:
        assert hashlib.sha256((vault / relative).read_bytes()).hexdigest() == expected_hash
    assert (vault / validation.destination).exists()


def test_inactive_note_uses_structural_only_and_creates_no_review_artifact(vault: Path) -> None:
    result = note.note(
        vault,
        content="# Draft\n\nWork in progress.\n",
        note_type="insight",
        title="Structural draft",
        status="draft",
        today=TODAY,
    )

    assert result.creation["applicability"] == "structural"
    page = vault / result.path
    payload = page.read_text(encoding="utf-8")
    page_id = payload.split("exomem_id: ", 1)[1].splitlines()[0]
    assert not relation_review.review_artifact_path(vault, page_id).exists()


def test_link_without_connections_omits_relations_placeholder(vault: Path) -> None:
    result = link.link(
        vault,
        entity_type="concept",
        name="No Placeholder",
        summary="A structural entity.",
        today=TODAY,
    )

    text = (vault / result.path).read_text(encoding="utf-8")
    assert "## Relations" not in text
    assert "- (none yet)" not in text


def test_replace_validate_only_changes_neither_predecessor_nor_destination(
    vault: Path,
) -> None:
    predecessor = note.note(
        vault,
        content="# Replace draft predecessor\n\nDraft predecessor.\n",
        note_type="insight",
        title="Replace draft predecessor",
        status="draft",
        today=TODAY,
    )
    predecessor_path = vault / predecessor.path
    before = _tree_bytes(vault)

    validation = replace.replace(
        vault,
        old_path=predecessor.path,
        content="# Replace validation successor\n\nActive successor.\n",
        note_type="insight",
        title="Replace validation successor",
        today=TODAY,
        validate_only=True,
    )

    assert validation.mutated is False
    assert predecessor_path.read_bytes() == before[predecessor.path]
    assert not (vault / validation.destination).exists()
    assert _tree_bytes(vault) == before


def test_replace_rejects_inactive_successor_before_mutation(vault: Path) -> None:
    predecessor = note.note(
        vault,
        content="# Active predecessor draft\n\nDraft setup.\n",
        note_type="insight",
        title="Inactive replacement predecessor",
        status="draft",
        today=TODAY,
    )
    before = _tree_bytes(vault)

    with pytest.raises(replace.ReplaceError) as exc:
        replace.replace(
            vault,
            old_path=predecessor.path,
            content="# Inactive successor\n\nMust not supersede.\n",
            note_type="insight",
            title="Inactive replacement successor",
            status="draft",
            today=TODAY,
        )

    assert exc.value.code == "INACTIVE_SUCCESSOR"
    assert _tree_bytes(vault) == before


def test_replace_rejects_predecessor_drift_before_coordinator_commit(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    predecessor_kwargs = {
        "content": "# Guarded predecessor\n\nThe earlier conclusion.\n",
        "note_type": "insight",
        "title": "Guarded replacement predecessor",
        "today": TODAY,
    }
    predecessor_validation = note.note(
        vault, validate_only=True, **predecessor_kwargs
    )
    predecessor = note.note(
        vault,
        draft_id=predecessor_validation.draft_id,
        draft_hash=predecessor_validation.draft_hash,
        draft_token=predecessor_validation.draft_token,
        relation_disposition="reviewed_none",
        relation_review_hash=predecessor_validation.draft_hash,
        relation_review_reason="Fixture predecessor has no honest relation.",
        **predecessor_kwargs,
    )
    replacement_kwargs = {
        "old_path": predecessor.path,
        "content": "# Guarded successor\n\nThe replacement conclusion.\n",
        "note_type": "insight",
        "title": "Guarded replacement successor",
        "today": TODAY,
    }
    validation = replace.replace(vault, validate_only=True, **replacement_kwargs)
    predecessor_path = vault / predecessor.path
    real_commit = semantic_writes.commit_creation

    def drift_then_commit(*args: object, **kwargs: object):
        predecessor_path.write_text(
            predecessor_path.read_text(encoding="utf-8")
            + "\nConcurrent edit that must survive.\n",
            encoding="utf-8",
        )
        return real_commit(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(semantic_writes, "commit_creation", drift_then_commit)
    with pytest.raises(replace.ReplaceError) as exc:
        replace.replace(
            vault,
            draft_id=validation.draft_id,
            draft_hash=validation.draft_hash,
            draft_token=validation.draft_token,
            **replacement_kwargs,
        )

    assert exc.value.code in {"PATH_GUARD_CHANGED", "PATH_GUARD_CONTENT"}
    assert "Concurrent edit that must survive." in predecessor_path.read_text(
        encoding="utf-8"
    )
    assert not (vault / validation.destination).exists()


def test_superseded_recovery_rejects_mismatched_draft_token_with_pinned_precedence(
    vault: Path,
) -> None:
    old_rel = "Knowledge Base/Notes/Insights/token-bound-predecessor.md"
    destination = "Knowledge Base/Notes/Insights/token-bound-successor.md"
    draft_id = "00000000-0000-4000-8000-000000000099"
    original_token = semantic_writes.DraftToken(
        "note", "replacement", destination, TODAY.isoformat()
    ).encode()
    altered_token = semantic_writes.DraftToken(
        "note",
        "replacement",
        destination,
        (TODAY + dt.timedelta(days=1)).isoformat(),
    ).encode()
    old_path = vault / old_rel
    old_path.parent.mkdir(parents=True, exist_ok=True)
    old_path.write_text(
        "---\n"
        "type: insight\n"
        "status: superseded\n"
        "exomem_id: 00000000-0000-4000-8000-000000000098\n"
        f'superseded_by: "[[{destination.removesuffix(".md")}]]"\n'
        "---\n"
        "# Token-bound predecessor\n",
        encoding="utf-8",
    )
    receipt = relation_review.review_artifact_path(vault, draft_id)
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "kind": "qualifying",
                "page_identity": draft_id,
                "page_path_at_review": destination,
                "content_fingerprint": "a" * 64,
                "draft_hash": "b" * 64,
                "auxiliary_hash": "c" * 64,
                "reason": None,
                "operation": "replacement",
                "draft_token_hash": hashlib.sha256(
                    original_token.encode("utf-8")
                ).hexdigest(),
                "predecessor_path": old_rel,
                "predecessor_content_hash": "d" * 64,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(replace.ReplaceError) as exc:
        replace.replace(
            vault,
            old_path=old_rel,
            content="# Token-bound successor\n\nMust not recover.\n",
            note_type="insight",
            title="Token-bound successor",
            today=TODAY,
            draft_id=draft_id,
            draft_hash="b" * 64,
            draft_token=altered_token,
        )

    assert exc.value.code == "ALREADY_SUPERSEDED"
    assert not (vault / destination).exists()


def test_replace_partial_after_predecessor_patch_recovers_exact_batch(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class SimulatedProcessDeath(BaseException):
        pass

    predecessor_kwargs = {
        "content": "# Recovery predecessor\n\nThe earlier conclusion.\n",
        "note_type": "insight",
        "title": "Replacement recovery predecessor",
        "today": TODAY,
    }
    predecessor_validation = note.note(
        vault, validate_only=True, **predecessor_kwargs
    )
    predecessor = note.note(
        vault,
        draft_id=predecessor_validation.draft_id,
        draft_hash=predecessor_validation.draft_hash,
        draft_token=predecessor_validation.draft_token,
        relation_disposition="reviewed_none",
        relation_review_hash=predecessor_validation.draft_hash,
        relation_review_reason="Fixture predecessor has no honest relation.",
        **predecessor_kwargs,
    )
    replacement_kwargs = {
        "old_path": predecessor.path,
        "content": "# Recovery successor\n\nThe replacement conclusion.\n",
        "note_type": "insight",
        "title": "Replacement recovery successor",
        "today": TODAY,
    }
    validation = replace.replace(vault, validate_only=True, **replacement_kwargs)
    commit_kwargs = {
        "draft_id": validation.draft_id,
        "draft_hash": validation.draft_hash,
        "draft_token": validation.draft_token,
    }
    real_batch = relation_review.vault.batch_atomic_write
    real_os_replace = vault_module.os.replace
    captured: list[tuple[tuple[str, str], ...]] = []

    def capture_batch(writes: object, **kwargs: object):
        detached = tuple(writes)  # type: ignore[arg-type]
        captured.append(
            tuple(
                (
                    write.path.relative_to(vault).as_posix(),
                    hashlib.sha256(write.content.encode("utf-8")).hexdigest(),
                )
                for write in detached
            )
        )
        return real_batch(detached, **kwargs)

    def die_after_predecessor(src: object, dst: object) -> None:
        real_os_replace(src, dst)
        if str(src).endswith(".tmp") and Path(dst) == vault / predecessor.path:
            raise SimulatedProcessDeath

    monkeypatch.setattr(relation_review.vault, "batch_atomic_write", capture_batch)
    monkeypatch.setattr(vault_module.os, "replace", die_after_predecessor)
    with pytest.raises(SimulatedProcessDeath):
        replace.replace(vault, **commit_kwargs, **replacement_kwargs)
    monkeypatch.setattr(vault_module.os, "replace", real_os_replace)

    assert not (vault / validation.destination).exists()
    receipt_path = relation_review.review_artifact_path(vault, validation.draft_id)
    before_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert before_receipt["kind"] == "qualifying"
    assert before_receipt["operation"] == "replacement"

    result = replace.replace(vault, **commit_kwargs, **replacement_kwargs)
    after_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    artifact_rel = receipt_path.relative_to(vault).as_posix()
    first_auxiliaries = tuple(
        item for item in captured[0][:-1] if item[0] != artifact_rel
    )
    second_auxiliaries = captured[1][:-1]

    assert first_auxiliaries == second_auxiliaries
    assert before_receipt["auxiliary_hash"] == after_receipt["auxiliary_hash"]
    assert result.creation["creation"]["relation_disposition"] == "qualifying_relation"
    assert (vault / result.new_path).exists()


def test_tier2_semantic_overwrite_is_deferred_on_either_side(vault: Path) -> None:
    arbitrary = "Knowledge Base/Identity/arbitrary-before.md"
    create_file.create_file(vault, path=arbitrary, content="# Arbitrary\n", today=TODAY)
    semantic = (
        "---\n"
        "type: insight\n"
        "status: draft\n"
        "created: 2026-07-14\n"
        "updated: 2026-07-14\n"
        "tags: []\n"
        "---\n"
        "# Semantic after\n"
    )
    with pytest.raises(create_file.CreateFileError) as after_exc:
        create_file.create_file(
            vault, path=arbitrary, content=semantic, overwrite=True, today=TODAY
        )
    assert after_exc.value.code == "SEMANTIC_OVERWRITE_NOT_WIRED"

    before_path = vault / "Knowledge Base/Notes/Insights/semantic-before.md"
    before_path.parent.mkdir(parents=True, exist_ok=True)
    before_path.write_text(semantic, encoding="utf-8")
    with pytest.raises(create_file.CreateFileError) as before_exc:
        create_file.create_file(
            vault,
            path=before_path.relative_to(vault).as_posix(),
            content="# Arbitrary after\n",
            overwrite=True,
            today=TODAY,
        )
    assert before_exc.value.code == "SEMANTIC_OVERWRITE_NOT_WIRED"


def test_tier2_draft_token_freezes_validation_date_across_commit_day(
    vault: Path,
) -> None:
    kwargs = {
        "path": "Knowledge Base/Notes/Insights/tier2-frozen-date.md",
        "content": "# Tier 2 frozen date\n\nAn inactive semantic draft.\n",
        "frontmatter": {"type": "insight", "status": "draft", "tags": []},
    }
    validation = create_file.create_file(
        vault, today=TODAY, validate_only=True, **kwargs
    )

    result = create_file.create_file(
        vault,
        today=TODAY + dt.timedelta(days=1),
        draft_id=validation.draft_id,
        draft_hash=validation.draft_hash,
        draft_token=validation.draft_token,
        **kwargs,
    )

    text = (vault / result.path).read_text(encoding="utf-8")
    assert "2026-07-14" in text
    assert "2026-07-15" not in text
    assert result.creation["applicability"] == "structural"


def test_note_draft_hash_binds_registration_intent(vault: Path) -> None:
    kwargs = {
        "content": "# Bound registration\n\nRegistration intent is reviewed too.\n",
        "note_type": "research-note",
        "title": "Bound registration intent",
        "project": "bound-registration-project",
        "project_category": "domain",
        "today": TODAY,
    }
    validation = note.note(vault, validate_only=True, **kwargs)
    decoded = semantic_writes.DraftToken.decode(validation.draft_token)
    registration = decoded.registrations[0]
    tampered = semantic_writes.DraftToken(
        decoded.writer,
        decoded.operation,
        decoded.destination,
        decoded.render_date,
        (
            semantic_writes.DraftRegistration(
                registration.key, "tampered-category", registration.folder
            ),
        ),
    ).encode()

    with pytest.raises(note.NoteError) as exc:
        note.note(
            vault,
            draft_id=validation.draft_id,
            draft_hash=validation.draft_hash,
            draft_token=tampered,
            relation_disposition="reviewed_none",
            relation_review_hash=validation.draft_hash,
            relation_review_reason="No honest relation exists in the fixture corpus.",
            **kwargs,
        )

    assert exc.value.code == "DRAFT_HASH_MISMATCH"
    assert not (vault / validation.destination).exists()


def test_note_exact_reviewed_none_retry_survives_later_inbound_relation(
    vault: Path,
) -> None:
    kwargs = {
        "content": "# Stable reviewed retry\n\nThe original disconnected conclusion.\n",
        "note_type": "insight",
        "title": "Stable reviewed retry",
        "today": TODAY,
    }
    validation = note.note(vault, validate_only=True, **kwargs)
    commit_fields = {
        "draft_id": validation.draft_id,
        "draft_hash": validation.draft_hash,
        "draft_token": validation.draft_token,
        "relation_disposition": "reviewed_none",
        "relation_review_hash": validation.draft_hash,
        "relation_review_reason": "No honest relation exists in the fixture corpus.",
    }
    note.note(vault, **commit_fields, **kwargs)
    inbound = vault / "Knowledge Base" / "Notes" / "Insights" / "later-inbound.md"
    inbound.write_text(
        "---\n"
        "type: insight\n"
        "status: active\n"
        "exomem_id: 00000000-0000-4000-8000-000000000097\n"
        "---\n"
        "# Later inbound\n\n"
        "## Relations\n"
        f"- supports [[{validation.destination.removesuffix('.md')}]]\n",
        encoding="utf-8",
    )
    before_retry = _tree_bytes(vault)

    with pytest.raises(note.NoteError) as replay:
        note.note(vault, **commit_fields, **kwargs)

    assert replay.value.code == "DRAFT_ALREADY_COMMITTED"
    assert _tree_bytes(vault) == before_retry


def test_draft_token_rejects_cross_writer_and_duplicate_json_keys(vault: Path) -> None:
    kwargs = {
        "content": "# Token context\n\nReject a token from another writer.\n",
        "note_type": "insight",
        "title": "Token context binding",
        "status": "draft",
        "today": TODAY,
    }
    validation = note.note(vault, validate_only=True, **kwargs)
    decoded = semantic_writes.DraftToken.decode(validation.draft_token)
    cross_writer = semantic_writes.DraftToken(
        "create_file",
        decoded.operation,
        decoded.destination,
        decoded.render_date,
        decoded.registrations,
    ).encode()
    with pytest.raises(note.NoteError) as cross_exc:
        note.note(
            vault,
            draft_id=validation.draft_id,
            draft_hash=validation.draft_hash,
            draft_token=cross_writer,
            **kwargs,
        )
    assert cross_exc.value.code == "INVALID_DRAFT_TOKEN"

    duplicate = (
        b'{"version":1,"writer":"note","writer":"note",'
        b'"operation":"create","destination":"Knowledge Base/x.md",'
        b'"render_date":"2026-07-14","registrations":[]}'
    )
    encoded = base64.urlsafe_b64encode(duplicate).decode("ascii").rstrip("=")
    with pytest.raises(semantic_writes.SemanticWriteError) as duplicate_exc:
        semantic_writes.DraftToken.decode(encoded)
    assert duplicate_exc.value.code == "INVALID_DRAFT_TOKEN"


@pytest.mark.parametrize("writer", ("note", "replace", "link", "create_file"))
def test_log_plan_errors_are_typed_and_never_expose_underlying_text(
    vault: Path, monkeypatch: pytest.MonkeyPatch, writer: str
) -> None:
    secret = "SECRET_LOG_PLAN_MARKER"

    def fail_log_plan(*args: object, **kwargs: object) -> object:
        raise ValueError(secret)

    if writer == "note":
        monkeypatch.setattr(note, "plan_log_writes", fail_log_plan)
        error_type = note.NoteError

        def invoke() -> object:
            return note.note(
                vault,
                content="# Sanitized note\n\nDraft.\n",
                note_type="insight",
                title="Sanitized note log failure",
                status="draft",
                today=TODAY,
            )

    elif writer == "replace":
        predecessor = note.note(
            vault,
            content="# Log predecessor\n\nDraft.\n",
            note_type="insight",
            title="Sanitized replace predecessor",
            status="draft",
            today=TODAY,
        )
        monkeypatch.setattr(replace, "plan_log_writes", fail_log_plan)
        error_type = replace.ReplaceError

        def invoke() -> object:
            return replace.replace(
                vault,
                old_path=predecessor.path,
                content="# Sanitized successor\n\nActive replacement.\n",
                note_type="insight",
                title="Sanitized replace successor",
                today=TODAY,
            )

    elif writer == "link":
        monkeypatch.setattr(link, "plan_log_writes", fail_log_plan)
        error_type = link.LinkError

        def invoke() -> object:
            return link.link(
                vault,
                entity_type="concept",
                name="Sanitized link log failure",
                summary="A structural entity.",
                today=TODAY,
            )

    else:
        monkeypatch.setattr(create_file, "plan_log_writes", fail_log_plan)
        error_type = create_file.CreateFileError

        def invoke() -> object:
            return create_file.create_file(
                vault,
                path="Knowledge Base/sanitized-log-failure.txt",
                content="arbitrary\n",
                today=TODAY,
            )

    with pytest.raises(error_type) as exc:
        invoke()

    assert exc.value.code == "LOG_PLAN_CONFLICT"
    assert secret not in exc.value.reason
    assert secret not in str(exc.value)


def test_v2_qualifying_receipt_is_internal_and_never_review_state(tmp_path: Path) -> None:
    page_id = "00000000-0000-4000-8000-000000000001"
    artifact = relation_review.review_artifact_path(tmp_path, page_id)
    artifact.parent.mkdir(parents=True)
    payload = {
        "schema_version": 2,
        "kind": "qualifying",
        "page_identity": page_id,
        "page_path_at_review": "Knowledge Base/Notes/Insights/first.md",
        "content_fingerprint": "a" * 64,
        "draft_hash": "b" * 64,
        "auxiliary_hash": "c" * 64,
        "reason": None,
        "operation": "create",
        "draft_token_hash": "d" * 64,
        "predecessor_path": None,
        "predecessor_content_hash": None,
    }
    artifact.write_text(
        json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )

    receipt = relation_review.load_creation_receipt(tmp_path, page_id)

    assert receipt is not None and receipt.kind == "qualifying"
    assert relation_review.load_relation_reviews(tmp_path) == ()
