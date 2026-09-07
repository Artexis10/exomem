from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


def _stored_plan() -> dict[str, object]:
    return {
        "version": 1,
        "title": "Stored plan",
        "steps": [
            {
                "step_id": "create",
                "kind": "create-note",
                "args": {
                    "title": "Stored conclusion",
                    "slug": "stored-conclusion",
                    "content": (
                        "## Observations\n\n"
                        "- [decision] Preserve immutable intent. ^immutable-intent\n"
                    ),
                    "relation_disposition": "reviewed_none",
                    "relation_review_reason": (
                        "No honest relation exists for this isolated fixture."
                    ),
                },
            }
        ],
    }


def test_forward_plan_is_create_only_and_collision_refuses(vault: Path) -> None:
    from exomem import curation

    store = curation.CurationStore(vault)
    stored = store.create_forward(_stored_plan(), binding_manifest=[], registry_ids={})
    same = store.create_forward(_stored_plan(), binding_manifest=[], registry_ids={})

    assert same == stored
    assert store.plan_path(stored["run_id"]).read_bytes() == curation.canonical_json_bytes(
        stored["plan"]
    )

    changed = dict(stored["plan"])
    changed["title"] = "Changed bytes"
    with pytest.raises(curation.CurationError, match="CURATION_PLAN_COLLISION"):
        store._create_plan_at(store.plan_path(stored["run_id"]), changed)


def test_store_refuses_symlinked_run_ancestors(vault: Path, tmp_path: Path) -> None:
    from exomem import curation

    runs = vault / "Knowledge Base" / "_Governance" / "curation" / "runs"
    runs.parent.mkdir(parents=True, exist_ok=True)
    external = tmp_path / "external"
    external.mkdir()
    os.symlink(external, runs, target_is_directory=True)

    with pytest.raises(curation.CurationError, match="CURATION_PATH_UNSAFE"):
        curation.CurationStore(vault).create_forward(
            _stored_plan(), binding_manifest=[], registry_ids={}
        )
    assert list(external.iterdir()) == []


def test_store_read_holds_ancestors_when_run_is_swapped_to_external_symlink(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import curation

    store = curation.CurationStore(vault)
    stored = store.create_forward(_stored_plan(), binding_manifest=[], registry_ids={})
    run_dir = store.run_dir(stored["run_id"])
    parked = run_dir.with_name(f"{run_dir.name}-parked")
    external = tmp_path / "external-run"
    external.mkdir()
    (external / "plan.json").write_text('{"outside":"must-not-be-read"}', encoding="utf-8")

    real_assert = curation.CurationStore._assert_safe
    swapped = False

    def swap_after_path_check(self, target: Path) -> None:  # noqa: ANN001
        nonlocal swapped
        real_assert(self, target)
        if not swapped and target == store.plan_path(stored["run_id"]):
            run_dir.rename(parked)
            os.symlink(external, run_dir, target_is_directory=True)
            swapped = True

    monkeypatch.setattr(curation.CurationStore, "_assert_safe", swap_after_path_check)

    with pytest.raises(curation.CurationError, match="CURATION_PATH_UNSAFE"):
        store._read_json(store.plan_path(stored["run_id"]))
    assert swapped


def test_store_publication_boundary_refuses_relocation_before_any_run_artifact(
    vault: Path,
) -> None:
    from exomem import curation

    plan = {
        "version": 1,
        "title": "Unsupported relocation",
        "steps": [
            {
                "step_id": "move",
                "kind": "move",
                "args": {
                    "old_path": "Knowledge Base/Notes/Insights/old.md",
                    "new_path": "Knowledge Base/Notes/Insights/new.md",
                },
            }
        ],
    }
    store = curation.CurationStore(vault)
    with pytest.raises(curation.CurationError, match="CURATION_RENAME_HISTORY_UNPROVABLE"):
        store.create_forward(plan, binding_manifest=[], registry_ids={})
    assert not store.root.exists()

    sealed = {
        **curation.validate_forward_plan(plan),
        "binding_manifest": [],
        "registry_ids": {},
    }
    direct_run_id = "cur-20260901-111111111111"
    with pytest.raises(curation.CurationError, match="CURATION_RENAME_HISTORY_UNPROVABLE"):
        store._create_plan_at(store.plan_path(direct_run_id), sealed)
    assert not store.root.exists()


def test_state_reconstructs_from_immutable_approval_witness_and_receipt(vault: Path) -> None:
    from exomem import curation

    store = curation.CurationStore(vault)
    stored = curation.propose(vault, _stored_plan())
    run_id = stored["run_id"]
    curation.apply(
        vault,
        run_id=run_id,
        plan_id=stored["plan_id"],
        expected_plan_fingerprint=stored["plan_fingerprint"],
        why="Approved exact immutable bytes.",
    )
    store.state_path(run_id).unlink(missing_ok=True)

    state = store.reconstruct(run_id)

    assert state["phase"] == "completed"
    assert state["committed_steps"] == ["create"]
    assert state["next_action"] is None


def test_competing_committed_receipts_block_reconstruction(vault: Path) -> None:
    from exomem import curation

    store = curation.CurationStore(vault)
    stored = store.create_forward(_stored_plan(), binding_manifest=[], registry_ids={})
    run_id = stored["run_id"]
    receipt_dir = store.receipts_dir(run_id) / "000-create"
    receipt_dir.mkdir(parents=True, exist_ok=True)
    for attempt, digest in ((1, "a" * 64), (2, "b" * 64)):
        (receipt_dir / f"{attempt:04d}.json").write_text(
            json.dumps(
                {
                    "attempt": attempt,
                    "outcome": "committed",
                    "operation_id": "c" * 64,
                    "result_digest": digest,
                }
            ),
            encoding="utf-8",
        )

    state = store.reconstruct(run_id)
    assert state["phase"] == "blocked"
    assert state["error_code"] == "CURATION_OUTCOME_UNCERTAIN"


def test_stored_plan_identity_is_anchored_and_tampering_refuses_preview(vault: Path) -> None:
    from exomem import curation

    store = curation.CurationStore(vault)
    stored = store.create_forward(_stored_plan(), binding_manifest=[], registry_ids={})
    path = store.plan_path(stored["run_id"])
    tampered = json.loads(path.read_text(encoding="utf-8"))
    tampered["title"] = "Tampered but schema-valid title"
    path.write_text(json.dumps(tampered), encoding="utf-8")

    with pytest.raises(curation.CurationError, match="CURATION_PLAN_IDENTITY_MISMATCH"):
        curation.preview(vault, run_id=stored["run_id"])


def test_forged_committed_receipt_without_matching_witness_is_refused(vault: Path) -> None:
    from exomem import curation

    store = curation.CurationStore(vault)
    stored = store.create_forward(_stored_plan(), binding_manifest=[], registry_ids={})
    run_id = stored["run_id"]
    operation = curation.operation_id(stored["plan_id"], 0, "create")
    store.create_approval(
        run_id,
        plan_id=stored["plan_id"],
        fingerprint=stored["plan_fingerprint"],
        why="Approve the exact stored plan.",
    )
    receipt_dir = store.receipts_dir(run_id) / "000-create"
    receipt_dir.mkdir(parents=True, exist_ok=True)
    (receipt_dir / "0001.json").write_text(
        json.dumps(
            {
                "version": 1,
                "attempt": 1,
                "ordinal": 0,
                "step_id": "create",
                "operation_id": operation,
                "outcome": "committed",
                "result_digest": "f" * 64,
                "effect": {
                    "kind": "create-note",
                    "path": "Knowledge Base/Notes/Insights/stored-conclusion.md",
                },
            }
        ),
        encoding="utf-8",
    )

    state = store.reconstruct(run_id)

    assert state["phase"] == "blocked"
    assert state["error_code"] == "CURATION_OUTCOME_UNCERTAIN"
