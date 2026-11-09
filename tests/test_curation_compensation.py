from __future__ import annotations

from pathlib import Path

import pytest
from test_curation_execution import _apply, _create_step, _propose, _seed_note

from exomem import curation
from exomem.vault import content_hash


def _edit_step(
    vault: Path, step_id: str, slug: str, *, old: str = "old text", new: str = "new text"
) -> tuple[str, dict[str, object]]:
    path = _seed_note(vault, slug, f"Keep {old}.")
    source = (vault / path).read_text(encoding="utf-8")
    return path, {
        "step_id": step_id,
        "kind": "edit",
        "args": {
            "path": path,
            "why": "Apply reviewed wording.",
            "operation": {
                "kind": "replace_string",
                "old_string": old,
                "new_string": new,
                "expected_hash": content_hash(source),
                "relation_disposition": "reviewed_none",
                "relation_review_reason": "No relation changes.",
            },
        },
    }


def test_compensation_mapping_is_closed_and_reverses_committed_order() -> None:
    forward = [
        {"step_id": "create-note", "kind": "create-note"},
        {"step_id": "create-entity", "kind": "create-entity"},
        {"step_id": "relation", "kind": "accept-relation"},
        {"step_id": "edit", "kind": "edit"},
        {"step_id": "supersede", "kind": "supersede"},
        {"step_id": "move", "kind": "move"},
        {"step_id": "delete", "kind": "delete"},
        {"step_id": "recover", "kind": "recover"},
    ]

    assert curation.compensation_descriptors(forward) == [
        {"forward_step_id": "recover", "kind": "delete"},
        {"forward_step_id": "delete", "kind": "recover"},
        {"forward_step_id": "move", "kind": "move"},
        {"forward_step_id": "supersede", "kind": "supersede"},
        {"forward_step_id": "edit", "kind": "supersede"},
        {"forward_step_id": "relation", "kind": "supersede"},
        {"forward_step_id": "create-entity", "kind": "delete"},
        {"forward_step_id": "create-note", "kind": "delete"},
    ]


def test_compensation_is_distinct_reviewed_plan_and_runs_one_step_per_request(
    vault: Path,
) -> None:
    first_path, first_step = _edit_step(vault, "first", "compensation-first")
    second_path, second_step = _edit_step(vault, "second", "compensation-second")
    proposed = _propose(
        vault,
        first_step,
        second_step,
    )
    _apply(vault, proposed)
    curation.resume(vault, run_id=proposed["run_id"], plan_id=proposed["plan_id"])
    forward_evidence = sorted(
        curation.CurationStore(vault).evidence_dir(proposed["run_id"]).glob("*.json")
    )

    compensation = curation.propose_compensation(vault, run_id=proposed["run_id"])

    assert compensation["plan_id"] != proposed["plan_id"]
    assert compensation["plan_fingerprint"] != proposed["plan_fingerprint"]
    assert compensation["forward_plan_id"] == proposed["plan_id"]
    assert [item["args"]["old_path"] for item in compensation["plan"]["steps"]] == [
        second_path,
        first_path,
    ]

    first = curation.apply_compensation(
        vault,
        run_id=proposed["run_id"],
        plan_id=compensation["plan_id"],
        expected_plan_fingerprint=compensation["plan_fingerprint"],
        why="Approved separate compensation plan.",
    )
    assert first["phase"] == "compensating"
    assert "new text" in (vault / second_path).read_text(encoding="utf-8")
    assert (vault / first_path).is_file()

    second = curation.resume(vault, run_id=proposed["run_id"], plan_id=compensation["plan_id"])
    assert second["phase"] == "compensated"
    assert "new text" in (vault / first_path).read_text(encoding="utf-8")
    assert all(path.is_file() for path in forward_evidence)
    assert curation.CurationStore(vault).plan_path(proposed["run_id"]).is_file()


def test_compensation_refuses_later_drift_without_overwriting_it(vault: Path) -> None:
    path, step = _edit_step(vault, "edited", "compensation-drift")
    proposed = _propose(vault, step)
    _apply(vault, proposed)
    compensation = curation.propose_compensation(vault, run_id=proposed["run_id"])
    target = vault / path
    source = target.read_text(encoding="utf-8")
    target.write_text(source.replace("new text", "after later work"), encoding="utf-8")

    with pytest.raises(curation.CurationError, match="CURATION_BINDING_STALE"):
        curation.apply_compensation(
            vault,
            run_id=proposed["run_id"],
            plan_id=compensation["plan_id"],
            expected_plan_fingerprint=compensation["plan_fingerprint"],
            why="Do not overwrite later work.",
        )
    assert "after later work" in target.read_text(encoding="utf-8")


def test_compensation_proposal_refuses_drift_from_forward_witness(vault: Path) -> None:
    path, step = _edit_step(vault, "edited", "compensation-proposal-drift")
    proposed = _propose(vault, step)
    _apply(vault, proposed)
    target = vault / path
    target.write_text(
        target.read_text(encoding="utf-8").replace("new text", "changed later"),
        encoding="utf-8",
    )

    with pytest.raises(curation.CurationError, match="CURATION_OUTCOME_UNCERTAIN"):
        curation.propose_compensation(vault, run_id=proposed["run_id"])

    assert "changed later" in target.read_text(encoding="utf-8")


def test_content_compensation_after_leaf_crash_recovers_without_second_effect(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _path, step = _edit_step(vault, "edited", "compensation-crash")
    proposed = _propose(vault, step)
    _apply(vault, proposed)
    compensation = curation.propose_compensation(vault, run_id=proposed["run_id"])

    def fault(name: str) -> None:
        if name == "after-compensation-leaf-witness":
            raise curation.CurationFault(name)

    monkeypatch.setattr(curation, "_fault_barrier", fault)
    with pytest.raises(curation.CurationFault, match="after-compensation-leaf-witness"):
        curation.apply_compensation(
            vault,
            run_id=proposed["run_id"],
            plan_id=compensation["plan_id"],
            expected_plan_fingerprint=compensation["plan_fingerprint"],
            why="Approve crash-tested compensation.",
        )
    successors_before = sorted(
        (vault / "Knowledge Base/Notes/Insights").glob("compensate-*.md")
    )

    monkeypatch.setattr(curation, "_fault_barrier", lambda _name: None)
    recovered = curation.resume(vault, run_id=proposed["run_id"], plan_id=compensation["plan_id"])
    successors_after = sorted(
        (vault / "Knowledge Base/Notes/Insights").glob("compensate-*.md")
    )
    assert recovered["phase"] == "compensated"
    assert recovered["step"]["outcome"] == "recovered-committed"
    assert successors_after == successors_before


def test_status_reports_the_active_compensation_phase(vault: Path) -> None:
    _path, step = _edit_step(vault, "edited", "compensation-status")
    proposed = _propose(vault, step)
    _apply(vault, proposed)
    compensation = curation.propose_compensation(vault, run_id=proposed["run_id"])
    curation.apply_compensation(
        vault,
        run_id=proposed["run_id"],
        plan_id=compensation["plan_id"],
        expected_plan_fingerprint=compensation["plan_fingerprint"],
        why="Approve the active compensation.",
    )

    state = curation.status(vault, run_id=proposed["run_id"])

    assert state["phase"] == "compensated"
    assert state["plan_id"] == compensation["plan_id"]


def test_entity_edit_is_rejected_when_no_history_preserving_compensation_exists(
    vault: Path,
) -> None:
    entity = vault / "Knowledge Base/Entities/Concepts/curation-entity-edit.md"
    entity.parent.mkdir(parents=True, exist_ok=True)
    entity.write_text(
        "---\ntype: entity\nentity_type: concept\ntitle: Curation Entity\nstatus: active\n"
        "created: 2026-09-01\nupdated: 2026-09-01\n---\n\n# Curation Entity\n\nOld summary.\n",
        encoding="utf-8",
    )
    source = entity.read_text(encoding="utf-8")

    with pytest.raises(curation.CurationError, match="CURATION_COMPENSATION_UNAVAILABLE"):
        _propose(
            vault,
            {
                "step_id": "edit-entity",
                "kind": "edit",
                "args": {
                    "path": entity.relative_to(vault).as_posix(),
                    "why": "Attempt an uncompensable entity edit.",
                    "operation": {
                        "kind": "replace_string",
                        "old_string": "Old summary.",
                        "new_string": "New summary.",
                        "expected_hash": content_hash(source),
                    },
                },
            },
        )


def test_edit_compensation_uses_a_history_preserving_supersession(vault: Path) -> None:
    path = _seed_note(vault, "compensation-edit", "Keep old text.")
    before = (vault / path).read_text(encoding="utf-8")
    proposed = _propose(
        vault,
        {
            "step_id": "edit",
            "kind": "edit",
            "args": {
                "path": path,
                "why": "Apply reviewed new wording.",
                "operation": {
                    "kind": "replace_string",
                    "old_string": "old text",
                    "new_string": "new text",
                    "expected_hash": content_hash(before),
                    "relation_disposition": "reviewed_none",
                    "relation_review_reason": "No relation changes.",
                },
            },
        },
    )
    _apply(vault, proposed)
    compensation = curation.propose_compensation(vault, run_id=proposed["run_id"])

    result = curation.apply_compensation(
        vault,
        run_id=proposed["run_id"],
        plan_id=compensation["plan_id"],
        expected_plan_fingerprint=compensation["plan_fingerprint"],
        why="Restore the reviewed predecessor as a successor.",
    )

    successor = vault / result["step"]["path"]
    assert successor.is_file()
    assert "old text" in successor.read_text(encoding="utf-8")
    assert "new text" in (vault / path).read_text(encoding="utf-8")


def test_relocation_based_compensation_refuses_without_namespace_lineage(
    vault: Path,
) -> None:
    proposed = _propose(vault, _create_step("created", "compensation-relocation"))
    _apply(vault, proposed)

    with pytest.raises(
        curation.CurationError, match="CURATION_RENAME_HISTORY_UNPROVABLE"
    ):
        curation.propose_compensation(vault, run_id=proposed["run_id"])

    compensation_root = curation.CurationStore(vault).compensation_root(proposed["run_id"])
    assert not compensation_root.exists()


def test_fold_compensation_restores_every_episode_file_and_history_byte_for_byte(
    vault: Path, earlier_episode
) -> None:
    """Undo returns each revision to its path, retires only the step's entries, and leaves the head alone."""
    import json

    from test_curation_execution import _fold_step

    from exomem import compiled_history

    pages = earlier_episode(5)
    _seed_note(vault, "compensation-fold-cites", sources=[pages[1].removesuffix(".md")])
    episode_before = {path: (vault / path).read_bytes() for path in pages}
    history = vault / compiled_history.history_root()
    history_before = {path: path.read_bytes() for path in history.rglob("*.json")}
    proposed = _propose(vault, _fold_step(pages, vault))
    _apply(vault, proposed)
    assert len(list(history.rglob("*.json"))) == len(history_before) + 4
    assert not (vault / pages[0]).exists() and (vault / pages[1]).is_file()

    compensation = curation.propose_compensation(vault, run_id=proposed["run_id"])
    undone = curation.apply_compensation(
        vault,
        run_id=proposed["run_id"],
        plan_id=compensation["plan_id"],
        expected_plan_fingerprint=compensation["plan_fingerprint"],
        why="Approved separate compensation plan.",
    )

    assert undone["phase"] == "compensated"
    assert {path: (vault / path).read_bytes() for path in pages} == episode_before
    assert {path: path.read_bytes() for path in history.rglob("*.json")} == history_before
    retired = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in (vault / "Knowledge Base/_trash").rglob("*.json")
    ]
    assert sorted(item["path"] for item in retired if item["operation"] == "episode_fold") == sorted(pages[:-1])


@pytest.mark.parametrize("barrier", ["unfold-after-restore", "unfold-after-retire"])
def test_fold_compensation_cut_midway_resumes_to_the_same_restored_episode(
    vault: Path, earlier_episode, monkeypatch, barrier: str  # noqa: ANN001
) -> None:
    """A cut after a restore or after one entry's retirement resumes without losing or repeating anything."""
    from test_curation_execution import _fold_step, _ProcessCut

    from exomem import compiled_history

    pages = earlier_episode(4)
    episode_before = {path: (vault / path).read_bytes() for path in pages}
    history = vault / compiled_history.history_root()
    history_before = {path: path.read_bytes() for path in history.rglob("*.json")}
    proposed = _propose(vault, _fold_step(pages, vault))
    _apply(vault, proposed)
    compensation = curation.propose_compensation(vault, run_id=proposed["run_id"])

    def cut(name: str) -> None:
        if name == barrier:
            raise _ProcessCut(name)

    monkeypatch.setattr(curation, "_fault_barrier", cut)
    with pytest.raises(_ProcessCut):
        curation.apply_compensation(
            vault,
            run_id=proposed["run_id"],
            plan_id=compensation["plan_id"],
            expected_plan_fingerprint=compensation["plan_fingerprint"],
            why="Approved separate compensation plan.",
        )
    monkeypatch.setattr(curation, "_fault_barrier", lambda _name: None)
    resumed = curation.resume(vault, run_id=proposed["run_id"], plan_id=compensation["plan_id"])

    assert resumed["phase"] == "compensated"
    assert {path: (vault / path).read_bytes() for path in pages} == episode_before
    assert {path: path.read_bytes() for path in history.rglob("*.json")} == history_before


def test_duplicate_fold_compensation_restores_every_page_and_original_byte_for_byte(vault: Path, owner_scope) -> None:  # noqa: ANN001
    """Undoing a plan that folded two copies returns both copies, the canonical page and the citing note to their exact bytes."""
    from duplicate_fixtures import evidence_filing
    from test_curation_execution import _fold_duplicate_step

    data = b"\x89PNG photo of the notice"
    canonical = evidence_filing(vault, "Case", "letters", "notice.png", data)
    first = evidence_filing(vault, "Case", "exhibits", "exhibit-a.png", data)
    second = evidence_filing(vault, "Case", "annexes", "annex-a.png", data)
    citing = _seed_note(vault, "compensation-dup-cites", sources=[first.removesuffix(".md")])
    watched = [
        canonical, first, second, citing,
        *(page.removesuffix(".md") for page in (canonical, first, second)),
    ]
    before = {path: (vault / path).read_bytes() for path in watched}
    proposed = _propose(
        vault,
        _fold_duplicate_step(vault, canonical, first, step_id="first"),
        _fold_duplicate_step(vault, canonical, second, step_id="second"),
    )
    _apply(vault, proposed)
    curation.resume(vault, run_id=proposed["run_id"], plan_id=proposed["plan_id"])
    assert (vault / canonical).read_bytes() != before[canonical] and not (vault / first.removesuffix(".md")).exists()

    compensation = curation.propose_compensation(vault, run_id=proposed["run_id"])
    undone = curation.apply_compensation(
        vault,
        run_id=proposed["run_id"],
        plan_id=compensation["plan_id"],
        expected_plan_fingerprint=compensation["plan_fingerprint"],
        why="Approved separate compensation plan.",
    )
    while undone["phase"] != "compensated":
        undone = curation.resume(vault, run_id=proposed["run_id"], plan_id=compensation["plan_id"])

    assert {path: (vault / path).read_bytes() for path in watched} == before
