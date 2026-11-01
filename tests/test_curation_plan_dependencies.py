from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from exomem import commands, curation, temporal
from exomem.vault import content_hash

HOME = "Knowledge Base/Notes/Insights/curation-home.md"


def _open_obsidian_at_the_kb(vault: Path) -> None:
    # Links render KB-relative only when Obsidian opens the KB itself, which is
    # what makes a link to a not-yet-created page render differently once it lands.
    (vault / "Knowledge Base" / ".obsidian").mkdir(exist_ok=True)


def _create_step(step_id: str, slug: str, content: str) -> dict[str, object]:
    return {
        "step_id": step_id,
        "kind": "create-note",
        "args": {"title": f"Curation {slug}", "slug": slug, "content": content},
    }


def _home_step() -> dict[str, object]:
    step = _create_step(
        "home", "curation-home", "## Observations\n\n- [finding] The home keeps the record. ^home\n"
    )
    step["args"].update(
        relation_disposition="reviewed_none",
        relation_review_reason="The first page of the split has nothing to link to yet.",
    )
    return step


def _propose(vault: Path, *steps: dict[str, object]) -> dict[str, object]:
    return curation.propose(vault, {"version": 1, "title": "Split", "steps": list(steps)})


def _apply(vault: Path, proposed: dict[str, object]) -> dict[str, object]:
    return curation.apply(
        vault,
        run_id=proposed["run_id"],
        plan_id=proposed["plan_id"],
        expected_plan_fingerprint=proposed["plan_fingerprint"],
        why="Approved exact plan.",
    )


def _resume(vault: Path, proposed: dict[str, object]) -> dict[str, object]:
    return curation.resume(vault, run_id=proposed["run_id"], plan_id=proposed["plan_id"])


def _seed_note(vault: Path, slug: str, body: str) -> str:
    arguments = {"title": f"Curation {slug}", "slug": slug, "content": body}
    validation = commands.op_remember(vault, validate_only=True, **arguments)
    return commands.op_remember(
        vault,
        **arguments,
        draft_id=validation["draft_id"],
        draft_hash=validation["draft_hash"],
        draft_token=validation["draft_token"],
        relation_disposition="reviewed_none",
        relation_review_hash=validation["draft_hash"],
        relation_review_reason="No honest relation exists for this fixture.",
    )["path"]


def _edit_step(vault: Path, path: str, operation: dict[str, object]) -> dict[str, object]:
    before = (vault / path).read_text(encoding="utf-8")
    return {
        "step_id": "rewrite",
        "kind": "edit",
        "args": {
            "path": path,
            "why": "Tighten the finding.",
            "operation": {
                **operation,
                "expected_hash": content_hash(before),
                "relation_disposition": "reviewed_none",
                "relation_review_reason": "No relation changes.",
            },
        },
    }


def _knowledge_base_files(vault: Path) -> dict[str, bytes]:
    """Every Knowledge Base file except the curation run store, which records the refusal."""
    runs = vault / "Knowledge Base" / "_Governance" / "curation"
    return {
        path.relative_to(vault).as_posix(): path.read_bytes()
        for path in (vault / "Knowledge Base").rglob("*")
        if path.is_file() and runs not in path.parents
    }


def _days_later(monkeypatch: pytest.MonkeyPatch, days: int) -> None:
    later = temporal.now() + dt.timedelta(days=days)
    monkeypatch.setattr(temporal, "now", lambda: later)


@pytest.mark.parametrize(
    "link",
    [f"[[{HOME.removesuffix('.md')}]]", "[[Curation curation-home]]"],
    ids=["path", "title"],
)
def test_a_split_whose_second_page_links_to_the_first_applies_end_to_end(
    vault: Path, link: str
) -> None:
    _open_obsidian_at_the_kb(vault)
    proposed = _propose(
        vault,
        _home_step(),
        _create_step(
            "branch",
            "curation-branch",
            "## Observations\n\n- [finding] The branch splits off. ^branch\n\n"
            f"## Connections\n\n- {link}\n",
        ),
    )

    assert _apply(vault, proposed)["step"]["path"] == HOME
    finished = _resume(vault, proposed)

    assert finished["phase"] == "completed"
    branch = (vault / finished["step"]["path"]).read_text(encoding="utf-8")
    assert "[[Notes/Insights/curation-home" in branch


def test_a_page_created_by_another_writer_after_proposal_still_refuses(vault: Path) -> None:
    _open_obsidian_at_the_kb(vault)
    branch = _create_step(
        "branch",
        "curation-branch",
        "## Observations\n\n- [finding] The branch splits off. ^branch\n\n"
        "## Connections\n\n- [[Curation curation-elsewhere]]\n",
    )
    branch["args"].update(
        relation_disposition="reviewed_none",
        relation_review_reason="Its only link points at a page that does not exist yet.",
    )
    proposed = _propose(vault, branch)
    _seed_note(vault, "curation-elsewhere", "## Observations\n\n- [finding] Elsewhere. ^else\n")

    with pytest.raises(curation.CurationError) as refused:
        _apply(vault, proposed)

    assert refused.value.code == "DRAFT_HASH_MISMATCH"
    assert not (vault / "Knowledge Base/Notes/Insights/curation-branch.md").exists()


@pytest.mark.parametrize(
    ("operation", "reviewed"),
    [
        (
            {"kind": "replace_string", "old_string": "Keep old text.", "new_string": "Keep new text."},
            "Keep new text.",
        ),
        (
            {"kind": "batch_replace", "edits": [{"old_string": "old text", "new_string": "new text"}]},
            "Keep new text.",
        ),
        (
            {"kind": "patch_frontmatter", "field": "summary", "value": "Keeps the target."},
            "summary: Keeps the target.",
        ),
    ],
    ids=["edit", "multi-edit", "frontmatter"],
)
def test_an_edit_approved_days_after_proposal_applies_the_reviewed_change(
    vault: Path, monkeypatch: pytest.MonkeyPatch, operation: dict[str, object], reviewed: str
) -> None:
    path = _seed_note(vault, "curation-target", "## Observations\n\n- [finding] Keep old text. ^target\n")
    proposed = _propose(vault, _edit_step(vault, path, operation))
    _days_later(monkeypatch, 5)

    applied = _apply(vault, proposed)

    assert applied["phase"] == "completed"
    edited = (vault / path).read_text(encoding="utf-8")
    assert reviewed in edited
    assert f"updated: {temporal.stamp(temporal.now())}" in edited


def test_a_late_edit_ignores_a_transition_token_copied_into_the_plan(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _seed_note(vault, "curation-target", "## Observations\n\n- [finding] Keep old text. ^target\n")
    step = _edit_step(
        vault,
        path,
        {"kind": "replace_string", "old_string": "Keep old text.", "new_string": "Keep new text."},
    )
    validated = commands.op_edit_memory(
        vault,
        path=path,
        why="Tighten the finding.",
        operation={**step["args"]["operation"], "validate_only": True},
    )
    step["args"]["operation"]["transition_token"] = validated["semantic"]["transition_token"]
    proposed = _propose(vault, step)
    _days_later(monkeypatch, 3)

    assert _apply(vault, proposed)["phase"] == "completed"
    assert "Keep new text." in (vault / path).read_text(encoding="utf-8")


def test_an_edit_whose_link_resolves_differently_at_approval_refuses_before_writing(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _open_obsidian_at_the_kb(vault)
    path = _seed_note(vault, "curation-target", "## Observations\n\n- [finding] Keep old text. ^target\n")
    proposed = _propose(
        vault,
        _edit_step(
            vault,
            path,
            {
                "kind": "replace_body",
                "new_body": "## Observations\n\n- [finding] Keep old text. ^target\n\n"
                "## Connections\n\n- [[Curation curation-elsewhere]]\n",
            },
        ),
    )
    _seed_note(vault, "curation-elsewhere", "## Observations\n\n- [finding] Elsewhere. ^else\n")
    before = _knowledge_base_files(vault)
    _days_later(monkeypatch, 2)

    with pytest.raises(curation.CurationError) as refused:
        _apply(vault, proposed)

    assert refused.value.code == "CURATION_BINDING_STALE"
    assert _knowledge_base_files(vault) == before
    state = curation.status(vault, run_id=proposed["run_id"])
    assert (state["phase"], state["failed_step"], state["retryable"]) == ("failed", "rewrite", False)


def test_a_step_bound_to_a_page_an_earlier_step_changes_is_refused_at_proposal(
    vault: Path,
) -> None:
    path = _seed_note(vault, "curation-target", "## Observations\n\n- [finding] Keep old text. ^target\n")
    first = _edit_step(
        vault,
        path,
        {"kind": "replace_string", "old_string": "Keep old text.", "new_string": "Keep new text."},
    )
    second = _edit_step(
        vault,
        path,
        {"kind": "replace_string", "old_string": "^target", "new_string": "^kept"},
    )
    second["step_id"] = "retag"

    with pytest.raises(curation.CurationError) as refused:
        _propose(vault, first, second)

    assert refused.value.code == "CURATION_STEP_CONFLICT"


def test_a_relation_disposition_on_a_step_linking_to_an_earlier_page_is_refused_at_proposal(
    vault: Path,
) -> None:
    _open_obsidian_at_the_kb(vault)
    branch = _create_step(
        "branch",
        "curation-branch",
        "## Observations\n\n- [finding] The branch splits off. ^branch\n\n"
        "## Connections\n\n- [[Curation curation-home]]\n",
    )
    branch["args"].update(
        relation_disposition="reviewed_none",
        relation_review_reason="The home does not exist yet.",
    )

    with pytest.raises(curation.CurationError) as refused:
        _propose(vault, _home_step(), branch)

    assert refused.value.code == "CURATION_STEP_CONFLICT"
    assert not (vault / HOME).exists()


def test_a_link_to_a_page_an_earlier_step_supersedes_is_refused_at_proposal(vault: Path) -> None:
    old = _seed_note(vault, "curation-shared", "## Observations\n\n- [finding] First reading. ^first\n")
    supersede = {
        "step_id": "successor",
        "kind": "supersede",
        "args": {
            "old_path": old,
            "title": "Curation curation-shared",
            "slug": "curation-shared-successor",
            "note_type": "insight",
            "content": "## Observations\n\n- [finding] Second reading. ^second\n",
            "reason": "The reading changed.",
        },
    }
    citing = _create_step(
        "citing",
        "curation-citing",
        "## Observations\n\n- [finding] Cites the reading. ^citing\n\n"
        "## Connections\n\n- [[Curation curation-shared]]\n",
    )

    with pytest.raises(curation.CurationError) as refused:
        _propose(vault, supersede, citing)

    assert refused.value.code == "CURATION_STEP_CONFLICT"


def test_a_relation_review_the_leaf_would_refuse_is_refused_at_proposal(vault: Path) -> None:
    target = _seed_note(vault, "curation-target", "## Observations\n\n- [finding] Exists. ^target\n")
    linked = _create_step(
        "linked",
        "curation-linked",
        "## Observations\n\n- [finding] Cites the target. ^linked\n\n"
        f"## Connections\n\n- [[{target.removesuffix('.md')}]]\n",
    )
    linked["args"].update(
        relation_disposition="reviewed_none",
        relation_review_reason="Claims no relation although it links one.",
    )

    with pytest.raises(curation.CurationError) as refused:
        _propose(vault, linked)

    assert refused.value.code == "RELATION_REVIEW_NOT_APPLICABLE"


def test_a_relation_review_on_a_page_the_leaf_does_not_review_still_applies(vault: Path) -> None:
    log = _create_step(
        "log", "curation-log", "## Observations\n\n- [finding] Recorded the take. ^log\n"
    )
    log["args"].update(
        note_type="production-log",
        medium="reels",
        recorded="2026-10-01",
        relation_disposition="reviewed_none",
        relation_review_reason="A production log carries no relations.",
    )

    assert _apply(vault, _propose(vault, log))["phase"] == "completed"


def test_a_created_page_missing_the_relation_review_it_needs_is_refused_at_proposal(
    vault: Path,
) -> None:
    _seed_note(vault, "curation-peer", "## Observations\n\n- [finding] A peer exists. ^peer\n")
    lone = _create_step("lone", "curation-lone", "## Observations\n\n- [finding] Alone. ^lone\n")

    with pytest.raises(curation.CurationError) as refused:
        _propose(vault, lone)

    assert refused.value.code == "SEMANTIC_CONTRACT_BLOCKED"
    assert "RELATION_DISPOSITION_MISSING" in str(refused.value)


def test_an_edit_that_stales_the_saved_relation_review_needs_a_review_at_proposal(
    vault: Path,
) -> None:
    home = _seed_note(
        vault, "curation-home", "## Observations\n\n- [finding] The home keeps the record. ^home\n"
    )
    _seed_note(vault, "curation-peer", "## Observations\n\n- [finding] A peer exists. ^peer\n")
    reviewed = _edit_step(
        vault,
        home,
        {
            "kind": "replace_string",
            "old_string": "keeps the record",
            "new_string": "keeps the whole record",
        },
    )
    unreviewed = json.loads(json.dumps(reviewed))
    for key in ("relation_disposition", "relation_review_reason"):
        unreviewed["args"]["operation"].pop(key)

    with pytest.raises(curation.CurationError) as refused:
        _propose(vault, unreviewed)

    assert refused.value.code == "SEMANTIC_CONTRACT_BLOCKED"
    assert "RELATION_DISPOSITION_STALE" in str(refused.value)
    assert _apply(vault, _propose(vault, reviewed))["phase"] == "completed"


def test_a_link_only_in_the_reason_for_an_edit_does_not_defer_its_relation_review(
    vault: Path,
) -> None:
    target = _seed_note(
        vault, "curation-target", "## Observations\n\n- [finding] Keeps the record. ^target\n"
    )
    _seed_note(vault, "curation-peer", "## Observations\n\n- [finding] A peer exists. ^peer\n")
    edit = _edit_step(
        vault,
        target,
        {
            "kind": "replace_string",
            "old_string": "Keeps the record",
            "new_string": "Keeps the whole record",
        },
    )
    for key in ("relation_disposition", "relation_review_reason"):
        edit["args"]["operation"].pop(key)
    edit["args"]["why"] = "Follow-up to [[Curation curation-home]]."

    with pytest.raises(curation.CurationError) as refused:
        _propose(vault, _home_step(), edit)

    assert refused.value.code == "SEMANTIC_CONTRACT_BLOCKED"
