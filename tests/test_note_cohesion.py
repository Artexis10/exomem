"""Guard tests for the note-cohesion question.

A compiled page that reaches a size step opens one review item, and the write that
leaves it there asks the writing agent one fixed question. These tests drive real
writes and the real triage surface on a copy of the fixture vault. Each names the
failure only it catches.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from exomem import audit as audit_module
from exomem import commands, due_state, mutation_terminal, note_cohesion, review_state, writer_lease

TODAY = dt.date(2026, 8, 16)
PAGE = "Knowledge Base/Notes/Research/Travel/seasonal-fare-scouting.md"
FAMILY_REF = "exomem://review/family/note_cohesion"


def _command(name: str):
    return next(command for command in commands.PRODUCT_COMMANDS if command.name == name)


def _unit(number: int) -> str:
    return (
        f"- [decision] Booking rule {number}: hold the fare for trip {number} "
        f"until the sale opens. #travel ^rule-{number}"
    )


def _page(units: int, *, tags: str = "travel", page_type: str = "research-note") -> str:
    body = "\n".join(_unit(number) for number in range(1, units + 1))
    return (
        "---\n"
        "title: Seasonal fare scouting\n"
        f"type: {page_type}\n"
        "project: travel\n"
        "status: active\n"
        "exomem_id: 00000000-0000-4000-8000-0000000005a1\n"
        "updated: 2026-08-15\n"
        f"tags: [{tags}]\n"
        "---\n\n"
        "# Seasonal fare scouting\n\n"
        f"## Observations\n\n{body}\n"
    )


def _put(vault: Path, rel: str, text: str) -> None:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _grow_by_one(vault: Path, number: int) -> dict:
    """One compiled write that adds the page's `number`th unit."""
    return writer_lease.invoke_command(
        _command("observe_memory"),
        vault,
        path=PAGE,
        operation="add",
        category="decision",
        content=f"Booking rule {number}: hold the fare for trip {number} until the sale opens.",
        tags=["travel"],
    )


def _note_cohesion_findings(vault: Path) -> list[str]:
    report = audit_module.audit(vault, categories=["note_cohesion"], today=TODAY)
    return [finding.path for finding in report.findings if finding.category == "note_cohesion"]


def test_a_write_that_reaches_the_base_step_asks_the_one_question(vault: Path) -> None:
    _put(vault, PAGE, _page(8))

    result = _grow_by_one(vault, 9)

    assert result["status"] == "committed"
    suggestion = result["structure_suggestion"]
    assert set(suggestion) == {
        "kind", "units", "step_units", "next_step_units", "ref", "fingerprint", "question",
    }
    assert (suggestion["kind"], suggestion["units"]) == ("note_cohesion", 9)
    assert (suggestion["step_units"], suggestion["next_step_units"]) == (9, 14)
    assert suggestion["question"] == note_cohesion.QUESTION
    assert suggestion["ref"].startswith("exomem://review/")


def test_a_write_below_the_base_step_asks_nothing(vault: Path) -> None:
    _put(vault, PAGE, _page(7))

    result = _grow_by_one(vault, 8)

    assert result["status"] == "committed"
    assert "structure_suggestion" not in result


def test_a_dismissal_holds_within_a_step_and_reopens_at_the_next(vault: Path) -> None:
    """The decision binds to the step. A fixed fingerprint would never reopen."""
    _put(vault, PAGE, _page(13))
    first = _grow_by_one(vault, 14)["structure_suggestion"]
    assert first["step_units"] == 14

    # Nobody answered, so the next write asks again about the same item.
    again = _grow_by_one(vault, 15)["structure_suggestion"]
    assert (again["ref"], again["fingerprint"]) == (first["ref"], first["fingerprint"])

    commands.op_triage_memory(
        vault,
        ref=first["ref"],
        action="dismiss",
        why="intentional: the page answers one question, the fare timing rules",
        expected_fingerprint=first["fingerprint"],
    )

    for number in range(16, 21):
        assert "structure_suggestion" not in _grow_by_one(vault, number)

    reopened = _grow_by_one(vault, 21)["structure_suggestion"]
    assert reopened["step_units"] == 21
    assert reopened["ref"] == first["ref"]
    assert reopened["fingerprint"] != first["fingerprint"]


def test_a_quiet_family_withholds_the_question_and_leaves_the_item_open(vault: Path) -> None:
    _put(vault, PAGE, _page(8))
    commands.op_triage_memory(
        vault, ref=FAMILY_REF, action="quiet", why="intentional: this vault keeps long pages"
    )

    result = _grow_by_one(vault, 9)

    assert result["status"] == "committed"
    assert "structure_suggestion" not in result
    assert _note_cohesion_findings(vault) == [PAGE]


def test_an_unreadable_review_state_omits_the_question_and_keeps_the_write(vault: Path) -> None:
    _put(vault, PAGE, _page(8))
    store = review_state.ReviewStateStore(vault)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{ not json", encoding="utf-8")

    result = _grow_by_one(vault, 9)

    assert result["status"] == "committed"
    assert result["mutated"] is True
    assert "structure_suggestion" not in result


def test_only_compiled_pages_without_a_breadth_tag_are_asked(vault: Path) -> None:
    """One eligible page and three that must never ask, all past the base step."""
    _put(vault, PAGE, _page(12))
    _put(vault, "Knowledge Base/Notes/Research/Travel/hub.md", _page(12, tags="travel, hub"))
    _put(
        vault,
        "Knowledge Base/Sources/Articles/fare-notes.md",
        _page(12, page_type="source"),
    )
    _put(vault, "Knowledge Base/Notes/Research/Travel/index.md", _page(12))

    assert _note_cohesion_findings(vault) == [PAGE]


def test_a_page_write_opens_and_clears_the_pages_backlog_entry(vault: Path) -> None:
    """The projection must settle the page on the write, with no vault-wide recompute."""
    _put(vault, PAGE, _page(8))
    due_state.reconcile(vault, today=TODAY)
    assert PAGE not in (due_state.load(vault)["categories"].get("note_cohesion") or {})

    _grow_by_one(vault, 9)
    assert PAGE in due_state.load(vault)["categories"]["note_cohesion"]

    writer_lease.invoke_command(
        _command("edit_memory"),
        vault,
        path=PAGE,
        operation={
            "kind": "edit_section",
            "heading": "## Observations",
            "section_position": "replace",
            "new_string": "\n".join(_unit(number) for number in range(1, 4)),
        },
        why="move the booking rules to their own note",
    )
    assert PAGE not in (due_state.load(vault)["categories"].get("note_cohesion") or {})


def test_the_backlog_serves_after_dated_obligations_and_before_the_source_backlog(
    vault: Path,
) -> None:
    _put(vault, PAGE, _page(10))
    _put(
        vault,
        "Knowledge Base/Notes/Insights/old-question.md",
        "---\ntitle: old-question\ntype: insight\nstatus: active\n"
        "created: 2026-05-01\nupdated: 2026-05-01\n---\n\n"
        "## Open Question\n\n- id: q1\n\nDoes the projection survive a day boundary?\n",
    )
    due_state.reconcile(vault, today=TODAY)

    categories = [row["category"] for row in due_state.served_entries(vault, today=TODAY)]

    assert {"question_aging", "note_cohesion", "unprocessed_source"} <= set(categories)
    first = {name: categories.index(name) for name in set(categories)}
    assert first["question_aging"] < first["note_cohesion"] < first["unprocessed_source"]


_VALID = {
    "kind": "note_cohesion",
    "units": 14,
    "step_units": 14,
    "next_step_units": 21,
    "ref": "exomem://review/0123456789abcdef01234567",
    "fingerprint": "0123456789abcdef01234567",
    "question": note_cohesion.QUESTION,
}


def _compact(suggestion: dict) -> dict:
    leaf = {"semantic": {"path": PAGE, "structure_suggestion": suggestion}}
    terminal = mutation_terminal.committed_terminal(
        leaf, request_id="r", receipt_id=None, idempotency_key=None
    )
    return mutation_terminal.project_terminal(terminal, "compact")


def test_the_response_layer_forwards_a_valid_question_unchanged() -> None:
    assert _compact(_VALID)["structure_suggestion"] == _VALID


@pytest.mark.parametrize(
    "suggestion",
    [
        {
            "kind": "scope_divergence",
            "strength": "strong",
            "reasons": ["off_scope_cluster_recurs"],
            "off_scope_units": 5,
            "cluster_terms": ["land", "property"],
        },
        {**_VALID, "step_units": 8, "units": 8},
        {**_VALID, "units": 13},
        {**_VALID, "next_step_units": 14},
        {**_VALID, "ref": "exomem://memory/0123456789abcdef01234567"},
        {**_VALID, "fingerprint": "not-a-fingerprint"},
        {**_VALID, "question": "x" * 5000},
    ],
    ids=[
        "removed-kind",
        "below-base-step",
        "units-below-step",
        "next-step-not-after-step",
        "not-a-review-ref",
        "malformed-fingerprint",
        "oversized-question",
    ],
)
def test_the_response_layer_drops_a_payload_outside_the_declared_bounds(
    suggestion: dict,
) -> None:
    assert "structure_suggestion" not in _compact(suggestion)
