from __future__ import annotations

import datetime as dt
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
    proposed = _propose(
        vault,
        _create_step(
            "branch",
            "curation-branch",
            "## Observations\n\n- [finding] The branch splits off. ^branch\n\n"
            "## Connections\n\n- [[Curation curation-elsewhere]]\n",
        ),
    )
    _seed_note(vault, "curation-elsewhere", "## Observations\n\n- [finding] Elsewhere. ^else\n")

    with pytest.raises(curation.CurationError) as refused:
        _apply(vault, proposed)

    assert refused.value.code == "DRAFT_HASH_MISMATCH"
    assert not (vault / "Knowledge Base/Notes/Insights/curation-branch.md").exists()


def test_an_edit_approved_days_after_proposal_applies_the_reviewed_change(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _seed_note(vault, "curation-target", "## Observations\n\n- [finding] Keep old text. ^target\n")
    proposed = _propose(
        vault,
        _edit_step(
            vault,
            path,
            {"kind": "replace_string", "old_string": "Keep old text.", "new_string": "Keep new text."},
        ),
    )
    _days_later(monkeypatch, 5)

    applied = _apply(vault, proposed)

    assert applied["phase"] == "completed"
    edited = (vault / path).read_text(encoding="utf-8")
    assert "Keep new text." in edited
    assert f"updated: {temporal.stamp(temporal.now())}" in edited


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
    before = (vault / path).read_bytes()
    _seed_note(vault, "curation-elsewhere", "## Observations\n\n- [finding] Elsewhere. ^else\n")
    _days_later(monkeypatch, 2)

    with pytest.raises(curation.CurationError) as refused:
        _apply(vault, proposed)

    assert refused.value.code == "CURATION_BINDING_STALE"
    assert (vault / path).read_bytes() == before
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
