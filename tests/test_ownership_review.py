"""Guard tests for the ownership review.

A page in a project can be asked whether it belongs in its current home: when it is
created there, when its project key or title changes, and when the daily rotation reaches
it. These tests drive real writes, the real triage surface and the real projection on a
copy of the fixture vault. Each names the failure only it catches.
"""

from __future__ import annotations

import datetime as dt
import json
import threading
from pathlib import Path

import numpy as np
import pytest

from exomem import (
    attention,
    capabilities,
    commands,
    curation,
    due_state,
    mutation_terminal,
    operation_entries,
    ownership_review,
    ownership_rotation,
    review_ids,
    review_state,
    semantic_authoring,
    writer_lease,
)
from exomem import audit as audit_module
from exomem.cli_ops import OpError
from exomem.governance.principal import request_scope
from exomem.vault import content_hash, parse_frontmatter
from tests.page_opens import pages_opened
from tests.test_consolidated_tools import _build, _call, _tool
from tests.test_derived_identifier_egress import _govern, _reset
from tests.test_episode_memory_command import _audience

TODAY = dt.date(2026, 8, 16)
FOLDER = "Knowledge Base/Notes/Research/Travel"
PAGE = f"{FOLDER}/seasonal-fare-scouting.md"
FAMILY_REF = "exomem://review/family/ownership_review"


def _command(name: str):
    return next(command for command in commands.PRODUCT_COMMANDS if command.name == name)


def _text(
    title: str = "Seasonal fare scouting",
    *,
    number: int = 1,
    project: str = "travel",
    tags: str = "travel",
    page_type: str = "research-note",
    status: str = "active",
    updated: str = "2026-08-15",
    extra_units: int = 0,
) -> str:
    units = "\n".join(
        f"- [decision] Booking rule {n}: hold the fare for trip {n} until the sale opens. "
        f"#travel ^rule-{n}"
        for n in range(1, extra_units + 2)
    )
    return (
        "---\n"
        f"title: {title}\n"
        f"type: {page_type}\n"
        f"project: {project}\n"
        f"status: {status}\n"
        f"exomem_id: 00000000-0000-4000-8000-{number:012d}\n"
        f"updated: {updated}\n"
        f"tags: [{tags}]\n"
        "---\n\n"
        f"# {title}\n\n"
        f"## Observations\n\n{units}\n"
    )


def _put(vault: Path, rel: str, text: str) -> None:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _key(vault: Path, rel: str) -> str:
    """The page's ledger identity, read from the page on disk as the product reads it."""
    frontmatter = parse_frontmatter((vault / rel).read_text(encoding="utf-8"))[0]
    return ownership_rotation.page_key(rel, frontmatter)


def _ask(vault: Path, *paths: str, on: dt.date = TODAY) -> None:
    ownership_rotation.mark_asked(vault, [_key(vault, path) for path in paths], on=on)


def _create(vault: Path, title: str = "Quokka census", *, project: str = "travel") -> dict:
    """A compiled creation through the real command, as an agent makes it."""
    return writer_lease.invoke_command(
        _command("remember"),
        vault,
        title=title,
        content=(
            "## Observations\n\n- [finding] The quokka population grew near the harbour. #travel\n"
        ),
        projects=[project],
        summary="A census finding.",
        relation_disposition="reviewed_none",
        relation_review_reason="a standalone census",
        response_detail="full",
    )


def _grow(vault: Path, page: str, number: int) -> dict:
    return writer_lease.invoke_command(
        _command("observe_memory"),
        vault,
        path=page,
        operation="add",
        category="decision",
        content=f"Booking rule {number}: hold the fare for trip {number} until the sale opens.",
        tags=["travel"],
        response_detail="full",
    )


def _patch(vault: Path, page: str, field: str, value) -> dict:
    return writer_lease.invoke_command(
        _command("edit_memory"),
        vault,
        path=page,
        operation={"kind": "patch_frontmatter", "field": field, "value": value},
        why="the page now belongs to another project",
        response_detail="full",
    )


def _suggestion(result: dict) -> dict | None:
    return (result.get("creation") or result).get("structure_suggestion") or result.get(
        "structure_suggestion"
    )


def _findings(vault: Path) -> list[str]:
    report = audit_module.audit(vault, categories=["ownership_review"], today=TODAY)
    return sorted(f.path for f in report.findings if f.category == "ownership_review")


def _keep(vault: Path, asked: dict) -> dict:
    """The dismissal exactly as the write response spells it: by the item's id, through dispatch."""
    return writer_lease.invoke_command(
        _command("triage_memory"),
        vault,
        item_id=asked["id"],
        action="dismiss",
        why="intentional: this page serves the fare-scouting goal of the travel project",
    )


# --- the guidance ---------------------------------------------------------------------


def test_the_question_carries_the_guidance_routes_and_the_spelled_keep_call() -> None:
    routes = semantic_authoring.AUTHORING_CONTRACT.routes

    text = ownership_review.question(keep=ownership_review.keep_call("abc.def"))

    # Rewording the test is a guidance change; the item must follow it with no code change.
    assert routes["ownership_question"] in text
    assert routes["ownership_test"] in text
    assert routes["ownership_deferral"] in text
    assert "abc.def" in text
    assert "Do not change the page when you answer." in text


# --- opening an item ------------------------------------------------------------------


@pytest.mark.parametrize("tool_shape", operation_entries.TOOL_SHAPES)
def test_a_page_created_in_a_project_asks_the_ownership_question(
    vault: Path, tool_shape: str
) -> None:
    """On every tool shape the keep call is spelled per surface, and a question longer
    than the response layer's bound would drop the advisory without a trace."""
    surface = capabilities.ActiveSurfaceDescriptor(
        surface="mcp", profile="product", tier2_enabled=True, product_commands=(),
        tool_shape=tool_shape,
    )

    with capabilities.active_surface(surface):
        result = _create(vault)

    suggestion = _suggestion(result)
    assert suggestion is not None
    assert set(suggestion) == {"kind", "ref", "fingerprint", "id", "question"}
    assert suggestion["kind"] == "ownership"
    assert suggestion["ref"].startswith("exomem://review/")
    assert suggestion["question"].startswith(
        semantic_authoring.AUTHORING_CONTRACT.routes["ownership_question"]
    )
    # The keep answer is spelled out with this item's id, so an agent can act on it.
    assert suggestion["id"] in suggestion["question"]


def test_a_created_page_opens_an_item_that_asks_and_makes_no_claim(vault: Path) -> None:
    created = _create(vault)["path"]
    due_state.reconcile(vault, today=TODAY)

    report = audit_module.audit(vault, categories=["ownership_review"], today=TODAY)
    [item] = [f for f in report.findings if f.category == "ownership_review"]

    assert item.path == created
    assert item.severity == "info"
    question = semantic_authoring.AUTHORING_CONTRACT.routes["ownership_question"]
    assert item.detail.startswith(question)
    assert "triage_memory" in item.detail
    # No score and no verdict: the runtime asks, and the connected agent judges.
    assert set(item.meta) == {
        "signal_version", "review_partition", "project", "home_folder", "due_since",
    }
    assert created in due_state.load(vault)["categories"]["ownership_review"]


def test_a_successor_page_asks_the_ownership_question_and_opens_its_item(vault: Path) -> None:
    """A supersede is the write that can carry a wrong project forward, so a successor is
    asked like any creation, through the same door and with its item in the projection."""
    _put(vault, PAGE, _text())
    due_state.reconcile(vault, today=TODAY)
    surface = capabilities.ActiveSurfaceDescriptor(
        surface="mcp", profile="product", tier2_enabled=True, product_commands=(),
        tool_shape=operation_entries.TOOL_SHAPES[0],
    )

    with capabilities.active_surface(surface):
        result = writer_lease.invoke_command(
            _command("replace_memory"),
            vault,
            old_path=PAGE,
            content=(
                "## Observations\n\n- [finding] Fares dip in the second week of March. #travel\n"
            ),
            title="Seasonal fare scouting v2",
            note_type="research-note",
            project="travel",
            relation_disposition="reviewed_none",
            relation_review_reason="a standalone rewrite",
            response_detail="full",
        )

    [successor] = [path for path in result["paths"] if path != PAGE]
    suggestion = _suggestion(result)
    assert suggestion is not None and suggestion["kind"] == "ownership"
    assert list(due_state.load(vault)["categories"]["ownership_review"]) == [successor]


def test_a_page_that_was_never_asked_has_no_item(vault: Path) -> None:
    _put(vault, PAGE, _text())

    assert _findings(vault) == []
    assert "structure_suggestion" not in _grow(vault, PAGE, 2)


def test_only_compiled_single_project_pages_in_a_live_status_are_ever_asked(vault: Path) -> None:
    """One eligible page, and the pages that must never ask even though they were marked."""
    pages = {
        PAGE: _text(number=1),
        f"{FOLDER}/hub.md": _text("Fare hub", number=2, tags="travel, hub"),
        f"{FOLDER}/index.md": _text("Fare index", number=3),
        "Knowledge Base/Sources/Articles/fare-notes.md": _text(
            "Fare notes", number=4, page_type="source"
        ),
        f"{FOLDER}/old-fares.md": _text("Old fares", number=5, status="superseded"),
        f"{FOLDER}/two-homes.md": _text("Two homes", number=6).replace(
            "project: travel", "projects: [travel, garden]"
        ),
    }
    for rel, text in pages.items():
        _put(vault, rel, text)
    _ask(vault, *pages)

    assert _findings(vault) == [PAGE]


# --- the decision ---------------------------------------------------------------------


def _open_rows(vault: Path) -> list[dict]:
    return [
        row
        for row in due_state.served_entries(vault, today=TODAY, include_triaged=True)
        if row["category"] == "ownership_review" and not row.get(due_state.TRIAGED)
    ]


def test_a_dismissal_holds_while_the_body_grows_and_reopens_on_a_title_or_project_change(
    vault: Path,
) -> None:
    """The decision binds to project key, folder and title. A body in the fingerprint would
    reopen every dismissal on the next edit; none of the three would never reopen it."""
    _put(vault, PAGE, _text())
    _ask(vault, PAGE)
    due_state.reconcile(vault, today=TODAY)
    [row] = _open_rows(vault)
    commands.op_triage_memory(
        vault,
        ref=row["ref"],
        action="dismiss",
        why="intentional: the page serves the fare-scouting goal of the travel project",
        expected_fingerprint=row["fingerprint"],
    )

    _put(vault, PAGE, _text(extra_units=12, updated="2026-08-16"))
    due_state.reconcile(vault, today=TODAY)
    assert _open_rows(vault) == []

    _put(vault, PAGE, _text("Fare timing rules", extra_units=12))
    due_state.reconcile(vault, today=TODAY)
    [retitled] = _open_rows(vault)
    assert retitled["ref"] == row["ref"]
    assert retitled["fingerprint"] != row["fingerprint"]

    commands.op_triage_memory(
        vault,
        ref=retitled["ref"],
        action="dismiss",
        why="intentional: still the fare-scouting goal",
        expected_fingerprint=retitled["fingerprint"],
    )
    _put(vault, PAGE, _text("Fare timing rules", project="garden", extra_units=12))
    due_state.reconcile(vault, today=TODAY)
    [moved] = _open_rows(vault)
    assert moved["fingerprint"] not in {row["fingerprint"], retitled["fingerprint"]}


def test_a_dismissal_does_not_follow_a_page_into_another_folder() -> None:
    """The folder is part of what the user was asked about; a moved page is asked again."""
    here = ownership_review.Subject("travel", "Knowledge Base/Notes/Research/Travel", "Fares")
    there = ownership_review.Subject("travel", "Knowledge Base/Notes/Research/Fares", "Fares")

    assert ownership_review.signal_version(here) != ownership_review.signal_version(there)


def test_a_retitle_or_project_change_opens_the_item_at_once_without_the_rotation(
    vault: Path,
) -> None:
    _put(vault, PAGE, _text())
    assert "structure_suggestion" not in _grow(vault, PAGE, 2)

    result = _patch(vault, PAGE, "project", "garden")

    suggestion = _suggestion(result)
    assert suggestion is not None and suggestion["kind"] == "ownership"
    assert ownership_rotation.asked(vault)[_key(vault, PAGE)] == dt.date.today()


MOVED = "Knowledge Base/Notes/Research/Fares/seasonal-fare-scouting.md"
RENAMED = f"{FOLDER}/fare-scouting-notes.md"


def _dismissed_page(vault: Path) -> dict:
    """A page that was asked and whose item the user dismissed; returns the dismissed row."""
    _put(vault, PAGE, _text())
    _ask(vault, PAGE)
    due_state.reconcile(vault, today=TODAY)
    [row] = _open_rows(vault)
    commands.op_triage_memory(
        vault,
        ref=row["ref"],
        action="dismiss",
        why="intentional: the page serves the fare-scouting goal of the travel project",
        expected_fingerprint=row["fingerprint"],
    )
    assert _open_rows(vault) == []
    return row


def test_a_dismissed_page_that_moves_to_another_folder_is_asked_again(vault: Path) -> None:
    """The ledger follows the page's id. Keyed by path, a moved page would drop out of it and
    the folder change could never reopen the dismissal."""
    dismissed = _dismissed_page(vault)

    (vault / MOVED).parent.mkdir(parents=True, exist_ok=True)
    commands.op_move_file(vault, PAGE, MOVED)

    # A move writes no due-state delta, so the first write to the page asks from its live
    # state and the projection catches up at the next reconcile.
    asked = _suggestion(_grow(vault, MOVED, 2))
    assert asked is not None and asked["kind"] == "ownership"
    assert asked["ref"] == dismissed["ref"]
    assert asked["fingerprint"] != dismissed["fingerprint"]
    due_state.reconcile(vault, today=TODAY)
    [reopened] = _open_rows(vault)
    assert (reopened["path"], reopened["fingerprint"]) == (MOVED, asked["fingerprint"])
    assert len(ownership_rotation.asked_path(vault).read_text().splitlines()) == 1


def test_a_rename_that_keeps_the_folder_and_title_keeps_the_dismissal(vault: Path) -> None:
    """Nothing the user was asked about changed. The triaged row at the new path proves the
    page is still asked: a path-keyed ledger would show no row and pass the quiet checks."""
    dismissed = _dismissed_page(vault)

    commands.op_move_file(vault, PAGE, RENAMED)
    due_state.reconcile(vault, today=TODAY)

    rows = [
        row
        for row in due_state.served_entries(vault, today=TODAY, include_triaged=True)
        if row["category"] == "ownership_review"
    ]
    states = [(r["path"], r["fingerprint"], bool(r.get(due_state.TRIAGED))) for r in rows]
    assert states == [(RENAMED, dismissed["fingerprint"], True)]
    assert _suggestion(_grow(vault, RENAMED, 2)) is None


def test_an_open_page_asks_again_on_each_write_and_an_answered_page_writes_quietly(
    vault: Path,
) -> None:
    _put(vault, PAGE, _text())
    _ask(vault, PAGE)
    due_state.reconcile(vault, today=TODAY)  # a live vault holds its projection

    first = _suggestion(_grow(vault, PAGE, 2))
    again = _suggestion(_grow(vault, PAGE, 3))
    assert first is not None and again is not None
    assert (again["ref"], again["fingerprint"]) == (first["ref"], first["fingerprint"])

    _keep(vault, again)

    assert _suggestion(_grow(vault, PAGE, 4)) is None


def test_dismissing_another_family_on_the_page_leaves_the_question_open(vault: Path) -> None:
    """The ownership item has its own id, so a decision on the page's relation debt settles
    the debt and leaves this question open."""
    _put(vault, PAGE, _text())
    _ask(vault, PAGE)
    asked = _suggestion(_grow(vault, PAGE, 2))
    relation_debt = _rows(vault, "relation_debt")

    commands.op_triage_memory(
        vault, ref=relation_debt[0]["ref"], action="dismiss", why="intentional: no links needed"
    )

    still = _suggestion(_grow(vault, PAGE, 3))
    assert still is not None
    assert (still["ref"], still["fingerprint"]) == (asked["ref"], asked["fingerprint"])


def _rows(vault: Path, *categories: str, state: str = "open") -> list[dict]:
    """The review rows of PAGE in the listing for these categories, or in the default listing."""
    listing = commands.op_attention(
        vault, categories=list(categories) or None, state=state, limit=0
    )
    return [item for item in listing["items"] if item["path"] == PAGE]


def test_a_page_with_ownership_and_relation_debt_lists_two_rows_whose_refs_resolve(
    vault: Path,
) -> None:
    """The ref a queue hands out must resolve to the row that carries its finding, in the
    default listing and in the queue's own listing. A merged row left the bare relation-debt
    ref resolving to nothing."""
    _put(vault, PAGE, _text())
    _ask(vault, PAGE)

    rows = {tuple(row["categories"]): row for row in _rows(vault)}

    assert set(rows) == {("ownership_review",), ("relation_debt",)}
    assert rows[("ownership_review",)]["ref"] != rows[("relation_debt",)]["ref"]
    for category in ("ownership_review", "relation_debt"):
        row = rows[(category,)]
        by_ref = attention.item_by_ref(vault, row["ref"])
        assert (by_ref.ref, by_ref.fingerprint, by_ref.categories) == (
            row["ref"],
            row["fingerprint"],
            [category],
        )
        own_queue = _rows(vault, category)
        assert [(r["ref"], r["fingerprint"]) for r in own_queue] == [
            (row["ref"], row["fingerprint"])
        ]


def test_keeping_the_page_from_the_default_listing_leaves_its_relation_debt_open(
    vault: Path,
) -> None:
    """Answering the ownership row settles the question and nothing else."""
    _put(vault, PAGE, _text())
    _ask(vault, PAGE)
    [row] = [r for r in _rows(vault) if r["categories"] == ["ownership_review"]]

    commands.op_triage_memory(
        vault,
        ref=row["ref"],
        action="dismiss",
        why="intentional: this page serves the fare-scouting goal of the travel project",
    )

    assert [r["categories"] for r in _rows(vault)] == [["relation_debt"]]
    assert [r["state"] for r in _rows(vault, "relation_debt")] == ["open"]
    assert _suggestion(_grow(vault, PAGE, 2)) is None


def test_a_relation_debt_dismissal_made_through_its_own_queue_holds_in_the_default_listing(
    vault: Path,
) -> None:
    _put(vault, PAGE, _text())
    _ask(vault, PAGE)
    [debt] = _rows(vault, "relation_debt")

    commands.op_triage_memory(
        vault, ref=debt["ref"], action="dismiss", why="intentional: no links needed"
    )

    assert [r["categories"] for r in _rows(vault)] == [["ownership_review"]]
    assert [r["state"] for r in _rows(vault, "relation_debt", state="all")] == ["dismissed"]


def test_a_dismissal_recorded_against_the_merged_row_still_binds_the_ownership_item(
    vault: Path,
) -> None:
    """Before the rows were separate, answering the page's one row recorded the decision
    under the ownership id, against the merged signals' fingerprint and against each signal's
    own. Those records are in users' stores, and the ownership row's fingerprint must still
    be one of them."""
    _put(vault, PAGE, _text())
    _ask(vault, PAGE)
    [ownership] = [r for r in _rows(vault) if r["categories"] == ["ownership_review"]]
    [debt] = _rows(vault, "relation_debt")
    asked_item = attention.item_by_ref(vault, ownership["ref"])
    debt_item = attention.item_by_ref(vault, debt["ref"])
    merged = attention.AttentionItem(
        path=PAGE,
        score=0.0,
        severity="info",
        categories=sorted(["ownership_review", "relation_debt"]),
        reasons=asked_item.reasons + debt_item.reasons,
        proposed_fix="",
        item_id=asked_item.item_id,
        target_ref=asked_item.target_ref,
    )
    merged.fingerprint = review_state.fingerprint(
        target_ref=asked_item.target_ref,
        categories=merged.categories,
        reasons=merged.reasons,
        related_refs=[],
    )
    assert merged.fingerprint not in {ownership["fingerprint"], debt["fingerprint"]}

    review_state.apply_for_item(vault, merged, action="dismiss", why="intentional: kept")

    assert [r["state"] for r in _rows(vault, "ownership_review", state="all")] == ["dismissed"]
    assert [r["state"] for r in _rows(vault, "relation_debt", state="all")] == ["open"]
    assert _suggestion(_grow(vault, PAGE, 2)) is None


def test_the_spelled_keep_call_dismisses_by_reading_only_its_own_page(vault: Path) -> None:
    """A binder that finds an id only by rebuilding the review queues reads every page in
    the vault for one decision, and the response told the agent to make this call after
    every write."""
    _put(vault, PAGE, _text())
    _ask(vault, PAGE)
    due_state.reconcile(vault, today=TODAY)
    asked = _suggestion(_grow(vault, PAGE, 2))

    with pages_opened(vault) as opened:
        _keep(vault, asked)

    assert opened <= {PAGE}
    assert _suggestion(_grow(vault, PAGE, 3)) is None


def test_a_restricted_caller_cannot_tell_a_withheld_page_id_from_no_id(vault: Path) -> None:
    """The stored entries are the owner's. A binder that answers every caller from them
    refuses the id of a withheld page differently from an id that does not exist, which
    tells a restricted caller that the page holds an open question."""
    withheld = "Knowledge Base/Notes/Insights/withheld-fare-rules.md"
    _put(
        vault,
        withheld,
        _text(page_type="insight", number=9).replace("project: travel", "projects: [travel]"),
    )
    _ask(vault, withheld)
    due_state.reconcile(vault, today=TODAY)
    asked = _suggestion(_grow(vault, withheld, 2))
    # The scope must match the page that exists: a withheld scope that names no page withholds nothing.
    _govern(vault, "client-a", scope="Notes/Insights/withheld-*.md")
    _reset()

    with request_scope(_audience("client-a")):
        with pytest.raises(OpError) as withheld_id:
            _keep(vault, asked)
        with pytest.raises(OpError) as no_such_id:
            _keep(vault, {"id": "aaaaaaa.bbbb"})

    assert withheld_id.value.code == no_such_id.value.code


def test_a_quiet_family_withholds_the_question_and_leaves_the_item_open(vault: Path) -> None:
    _put(vault, PAGE, _text())
    _ask(vault, PAGE)
    commands.op_triage_memory(
        vault, ref=FAMILY_REF, action="quiet", why="intentional: this vault is kept as it is"
    )

    result = _grow(vault, PAGE, 2)

    assert result["diagnostics"]["operational"]["status"] == "committed"
    assert _suggestion(result) is None
    assert _findings(vault) == [PAGE]


def test_the_structural_suggestions_class_withholds_the_question(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import envelope

    _put(vault, PAGE, _text())
    _ask(vault, PAGE)
    monkeypatch.setattr(envelope, "active", lambda *a, **k: {"structural_suggestions": "off"})

    result = _grow(vault, PAGE, 2)

    assert result["diagnostics"]["operational"]["status"] == "committed"
    assert _suggestion(result) is None


def test_an_unreadable_review_state_omits_the_question_and_keeps_the_write(vault: Path) -> None:
    _put(vault, PAGE, _text())
    _ask(vault, PAGE)
    store = review_state.ReviewStateStore(vault)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{ not json", encoding="utf-8")

    result = _grow(vault, PAGE, 2)

    assert result["diagnostics"]["operational"]["status"] == "committed"
    assert result["diagnostics"]["operational"]["mutated"] is True
    assert _suggestion(result) is None


def test_the_family_never_changes_the_page(vault: Path) -> None:
    """The question is advisory: asking, answering and rotating leave the page's bytes alone."""
    _put(vault, PAGE, _text())
    before = (vault / PAGE).read_bytes()
    _ask(vault, PAGE)
    due_state.reconcile(vault, today=TODAY)
    [row] = _open_rows(vault)

    commands.op_triage_memory(
        vault, ref=row["ref"], action="dismiss", why="intentional: the travel project's goal",
        expected_fingerprint=row["fingerprint"],
    )

    assert (vault / PAGE).read_bytes() == before


def test_a_write_asks_ownership_before_cohesion_and_only_one_question(vault: Path) -> None:
    """A page that is asked and at a size step carries one advisory, and ownership leads:
    the page's home decides what its size means."""
    _put(vault, PAGE, _text(extra_units=8))
    _ask(vault, PAGE)

    suggestion = _suggestion(_grow(vault, PAGE, 10))

    assert suggestion is not None and suggestion["kind"] == "ownership"


# --- the write path stays cheap --------------------------------------------------------


def test_the_write_path_reads_no_vectors_and_scans_no_project(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ask on a write is one ledger append. A distance or a project scan on the write
    path would put every vector read and every page parse inside the writer's latency."""

    def forbidden(*args, **kwargs):
        raise AssertionError("the write path must not do the rotation's work")

    monkeypatch.setattr(ownership_rotation, "page_means", forbidden)
    monkeypatch.setattr(ownership_rotation, "eligible_members", forbidden)
    monkeypatch.setattr(ownership_rotation, "rotate", forbidden)

    created = _create(vault)
    suggestion = _suggestion(created)

    assert suggestion is not None
    assert _key(vault, created["path"]) in ownership_rotation.asked(vault)


# --- the asked ledger ------------------------------------------------------------------


def test_a_torn_or_corrupt_ledger_line_never_loses_the_asked_pages(vault: Path) -> None:
    """A crash mid-append leaves a partial last line. Reading it as a page, or refusing the
    whole ledger, would either invent an item or silently forget every page asked."""
    _put(vault, PAGE, _text())
    _ask(vault, PAGE)
    key = _key(vault, PAGE)
    ledger = ownership_rotation.asked_path(vault)
    with ledger.open("ab") as handle:
        handle.write(b"not json\n")
        handle.write(b'{"key":"torn-key","on":"2026-0')

    assert dict(ownership_rotation.asked(vault)) == {key: TODAY}

    with ledger.open("ab") as handle:
        handle.write(b'8-16"}\n')

    assert set(ownership_rotation.asked(vault)) == {key, "torn-key"}


def test_asking_the_same_page_twice_keeps_its_first_day_and_adds_no_line(vault: Path) -> None:
    _put(vault, PAGE, _text())
    key = _key(vault, PAGE)
    ownership_rotation.mark_asked(vault, [key], on=TODAY)
    ownership_rotation.mark_asked(vault, [key], on=TODAY + dt.timedelta(days=3))

    assert ownership_rotation.asked(vault)[key] == TODAY
    assert len(ownership_rotation.asked_path(vault).read_text().splitlines()) == 1


def test_a_page_without_an_exomem_id_is_asked_by_its_path(vault: Path) -> None:
    """The fallback key: a page that has no id is still asked once and asks on each write."""
    no_id = _text().replace("exomem_id: 00000000-0000-4000-8000-000000000001\n", "")
    assert "exomem_id" not in no_id
    _put(vault, PAGE, no_id)

    assert _key(vault, PAGE) == PAGE
    _ask(vault, PAGE)

    assert list(ownership_rotation.asked(vault)) == [PAGE]
    assert _suggestion(_grow(vault, PAGE, 2)) is not None


# --- the backlog -----------------------------------------------------------------------


def test_the_backlog_serves_after_dated_obligations_and_before_cohesion_with_exact_counts(
    vault: Path,
) -> None:
    _put(vault, PAGE, _text(extra_units=10))
    _put(
        vault,
        "Knowledge Base/Notes/Insights/old-question.md",
        "---\ntitle: old-question\ntype: insight\nstatus: active\n"
        "created: 2026-05-01\nupdated: 2026-05-01\n---\n\n"
        "## Open Question\n\n- id: q1\n\nDoes the projection survive a day boundary?\n",
    )
    _ask(vault, PAGE)
    due_state.reconcile(vault, today=TODAY)

    served = due_state.served_entries(vault, today=TODAY)
    categories = [row["category"] for row in served]

    first = {name: categories.index(name) for name in set(categories)}
    assert first["question_aging"] < first["ownership_review"] < first["note_cohesion"]
    assert first["note_cohesion"] < first["unprocessed_source"]
    block = due_state.served(vault, today=TODAY)
    assert block["outstanding"]["ownership_review"] == 1
    assert block["outstanding"]["note_cohesion"] == 1


def test_a_creation_opens_the_pages_item_in_the_projection_without_a_recompute(
    vault: Path,
) -> None:
    due_state.reconcile(vault, today=TODAY)
    assert not (due_state.load(vault)["categories"].get("ownership_review") or {})

    created = _create(vault)["path"]

    assert created in due_state.load(vault)["categories"]["ownership_review"]


# --- the default listing and the curation route -----------------------------------------


def test_a_default_review_listing_shows_an_open_ownership_item_and_no_cohesion_item(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session that lists review items without naming a category meets the question here
    or not at all. A page at a size step also has a cohesion item, which stays opt-in."""
    _put(vault, PAGE, _text(extra_units=10))
    _ask(vault, PAGE)
    mcp = _build(monkeypatch)

    default = _tool(mcp, "list_review_items", {})
    named = _tool(mcp, "list_review_items", {"categories": ["note_cohesion"]})

    default_reasons = {
        reason["category"]
        for item in default["items"]
        if item["path"] == PAGE
        for reason in item["reasons"]
    }
    assert "ownership_review" in default_reasons
    assert "note_cohesion" not in default_reasons
    assert PAGE in {item["path"] for item in named["items"]}


def test_an_ownership_items_ref_opens_a_curation_work_item_on_its_page(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The agent answers the question from the listing, so the ref that listing gives must
    open the page for the curation route; it was refused as an entity candidate."""
    _put(vault, PAGE, _text())
    _ask(vault, PAGE)
    mcp = _build(monkeypatch)
    [item] = [
        i
        for i in _tool(mcp, "list_review_items", {})["items"]
        if i["path"] == PAGE and i["categories"] == ["ownership_review"]
    ]

    work = _call(
        mcp,
        "maintain_memory",
        {"mode": "curation", "curation_action": "work-item", "review_ref": item["ref"]},
    )

    assert "pages" in work, work
    [page] = work["pages"]
    assert page["path"] == PAGE
    assert page["content"] == (vault / PAGE).read_text(encoding="utf-8")
    assert "entity_candidate" not in work


def test_a_review_ref_that_names_no_item_keeps_the_entity_candidate_refusal(vault: Path) -> None:
    """Resolving a page item must not turn an unknown ref into an empty or different answer."""
    with pytest.raises(curation.CurationError, match="CURATION_ENTITY_CANDIDATE_STALE"):
        curation.work_item(vault, review_ref="exomem://review/" + "0" * 24)


# --- the rotation ----------------------------------------------------------------------


def _generate(vault: Path, project: str, count: int, *, first: int, updated: str = "2026-08-15"):
    paths = []
    for index in range(count):
        number = first + index
        rel = f"Knowledge Base/Notes/Research/{project.title()}/page-{number:03d}.md"
        _put(
            vault,
            rel,
            _text(f"Page {number}", number=number, project=project, tags=project, updated=updated),
        )
        paths.append(rel)
    return paths


def _rotate(vault: Path, generated: list[str], day: int = 0, *, limit: int, read_means=None):
    """One rotation over the generated pages only: the fixture vault has projects of its own.

    Returns the pages' paths, though the rotation returns their ledger keys.
    """
    pages = [
        page
        for page in audit_module._parse_all(vault / "Knowledge Base", vault)
        if page.rel_path in generated
    ]
    by_key = {
        ownership_rotation.page_key(page.rel_path, page.frontmatter): page.rel_path
        for page in pages
    }
    newly = ownership_rotation.rotate(
        vault, pages, today=TODAY + dt.timedelta(days=day), limit=limit, read_means=read_means
    )
    return [by_key[key] for key in newly]


def _project_of(path: str) -> str:
    return ownership_review.folder_of(path).rpartition("/")[2]


def _unit(direction: int, width: int = 8) -> np.ndarray:
    vector = np.zeros(width)
    vector[direction] = 1.0
    return vector


def test_the_farthest_group_in_a_project_is_asked_first(vault: Path) -> None:
    """The distance orders the rotation: six pages about another subject among forty."""
    paths = _generate(vault, "travel", 40, first=100)
    outliers = set(paths[:6])
    means = {
        path: _unit(1) if path in outliers else _unit(0) + 0.05 * _unit(2) for path in paths
    }

    asked = _rotate(vault, paths, limit=1, read_means=lambda root, wanted: means)

    assert len(asked) == 1 and asked[0] in outliers


def test_a_project_below_three_pages_asks_the_oldest_page_first(vault: Path) -> None:
    older = _generate(vault, "travel", 1, first=200, updated="2026-01-01")
    newer = _generate(vault, "travel", 1, first=201, updated="2026-08-01")

    assert _rotate(vault, older + newer, limit=1) == older


def test_one_large_project_cannot_take_the_whole_slice(vault: Path) -> None:
    paths = (
        _generate(vault, "travel", 40, first=300)
        + _generate(vault, "garden", 3, first=400)
        + _generate(vault, "kitchen", 3, first=410)
    )

    asked = _rotate(vault, paths, limit=4)

    projects = [_project_of(path) for path in asked]
    assert set(projects) == {"Travel", "Garden", "Kitchen"}
    assert projects.count("Travel") <= 2


def test_the_ring_resumes_after_the_last_project_asked(vault: Path) -> None:
    """With more projects than the daily slice, a ring that restarts at the first project
    would never reach the last one."""
    paths = [
        path
        for index, project in enumerate(("alpha", "bravo", "charlie", "delta"))
        for path in _generate(vault, project, 3, first=500 + index * 10)
    ]

    days = [_rotate(vault, paths, day, limit=2) for day in range(2)]

    assert {_project_of(path) for day in days for path in day} == {
        "Alpha", "Bravo", "Charlie", "Delta",
    }


def test_the_rotation_excludes_no_page_whatever_its_distance(vault: Path) -> None:
    """A filter would miss pages the distance ranks low; an order only delays them."""
    paths = _generate(vault, "travel", 8, first=600)
    means = {path: _unit(index % 3) for index, path in enumerate(paths[:6])}  # two have none

    seen: set[str] = set()
    for day in range(8):
        seen |= set(_rotate(vault, paths, day, limit=1, read_means=lambda root, wanted: means))

    assert seen == set(paths)
    assert ownership_rotation.never_asked_report(vault) == {
        "count": 0, "as_of": (TODAY + dt.timedelta(days=7)).isoformat(),
    }


def test_two_overlapping_rotations_take_one_days_slice(vault: Path) -> None:
    """A cold read starts the rotation and the projection rebuild, which rotates too, at the
    same moment. Each saw the day as untaken, so each asked a full slice."""
    paths = _generate(vault, "travel", 10, first=100)
    holding, release = threading.Event(), threading.Event()
    reads: list[int] = []

    def read_means(_root, _group):
        reads.append(1)
        if len(reads) == 1:
            holding.set()
            release.wait(10)
        return {}

    taken: list[list[str]] = []

    def run() -> None:
        taken.append(_rotate(vault, paths, limit=3, read_means=read_means))

    first, second = threading.Thread(target=run), threading.Thread(target=run)
    first.start()
    assert holding.wait(10)
    second.start()
    second.join(0.5)  # a run that is not held back passes the first one's day guard here
    release.set()
    first.join()
    second.join()

    assert sorted(len(slice_) for slice_ in taken) == [0, 3]
    assert len(ownership_rotation.asked(vault)) == 3


def test_the_rotation_runs_once_a_day_and_never_asks_a_page_twice(vault: Path) -> None:
    paths = _generate(vault, "travel", 6, first=700)

    first = _rotate(vault, paths, limit=2)
    same_day = _rotate(vault, paths, limit=2)
    next_day = _rotate(vault, paths, 1, limit=2)

    assert len(first) == 2 and same_day == []
    assert len(next_day) == 2 and not set(first) & set(next_day)


def test_a_membership_change_refreshes_only_that_project(vault: Path) -> None:
    """The refresh reads vectors for projects whose membership changed. Reading every
    project each day is the cost the bulk refresh exists to avoid."""
    paths = _generate(vault, "travel", 4, first=800) + _generate(vault, "garden", 4, first=810)
    read: list[list[str]] = []

    def means(root, wanted):
        read.append(sorted(wanted))
        return {}

    _rotate(vault, paths, limit=1, read_means=means)
    assert len(read) == 2
    read.clear()

    _rotate(vault, paths, 1, limit=1, read_means=means)
    assert read == []

    added = _generate(vault, "garden", 1, first=820)
    _rotate(vault, paths + added, 2, limit=1, read_means=means)
    assert len(read) == 1 and added[0] in read[0]
    assert all(_project_of(path) == "Garden" for path in read[0])


def test_an_off_rotation_asks_nothing(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ownership_rotation.ENV_ROTATION_DAILY, "0")
    paths = _generate(vault, "travel", 4, first=900)

    assert _rotate(vault, paths, limit=ownership_rotation.rotation_daily()) == []
    assert ownership_rotation.is_due(vault, TODAY) is False


def test_serving_due_state_runs_the_days_rotation_and_reports_what_is_left(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ownership_rotation.ENV_ROTATION_DAILY, "2")
    _generate(vault, "travel", 5, first=1000)
    eligible = sum(
        len(group)
        for group in ownership_rotation.eligible_members(
            vault, audit_module._parse_all(vault / "Knowledge Base", vault)
        ).values()
    )
    due_state.reconcile(vault, today=TODAY)
    assert len(ownership_rotation.asked(vault)) == 2

    tomorrow = TODAY + dt.timedelta(days=1)
    block = due_state.served(vault, today=tomorrow)

    assert block is not None
    assert len(ownership_rotation.asked(vault)) == 4
    assert block["outstanding"]["ownership_review"] == 4
    assert block["ownership_never_asked"] == {
        "count": eligible - 4, "as_of": tomorrow.isoformat(),
    }


# --- the response layer ----------------------------------------------------------------

_VALID = {
    "kind": "ownership",
    "ref": "exomem://review/0123456789abcdef01234567",
    "fingerprint": "0123456789abcdef01234567",
    "id": review_ids.item_id(
        {
            "ref": "exomem://review/0123456789abcdef01234567",
            "fingerprint": "0123456789abcdef01234567",
        }
    ),
    "question": "Ownership check: does this material belong in its current home?",
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
        {**_VALID, "ref": "exomem://memory/0123456789abcdef01234567"},
        {**_VALID, "fingerprint": "not-a-fingerprint"},
        {**_VALID, "question": ""},
        {**_VALID, "question": "x" * 5000},
    ],
    ids=["not-a-review-ref", "malformed-fingerprint", "empty-question", "oversized-question"],
)
def test_the_response_layer_drops_a_payload_outside_the_declared_bounds(
    suggestion: dict,
) -> None:
    assert "structure_suggestion" not in _compact(suggestion)


def test_a_real_question_fits_the_response_layer_bound() -> None:
    text = ownership_review.question(keep=ownership_review.keep_call("abcdefg.hijk"))

    assert 0 < len(text) <= mutation_terminal._MAX_OWNERSHIP_QUESTION_CHARS
    assert json.dumps({"question": text})


# --- the curation preview ---------------------------------------------------------------


def _child_step(step_id: str, title: str, project: str) -> dict:
    return {
        "step_id": step_id,
        "kind": "create-note",
        "args": {
            "title": title,
            "slug": title.lower().replace(" ", "-"),
            "note_type": "insight",
            "project": project,
            "content": "## Observations\n\n- [finding] A split-out finding. #travel ^split\n",
            "relation_disposition": "reviewed_none",
            "relation_review_reason": "a standalone split child",
        },
    }


def _propose_and_preview(vault: Path, steps: list[dict]) -> tuple[dict, dict]:
    command = _command("maintain_memory")
    proposed = writer_lease.invoke_command(
        command,
        vault,
        mode="curation",
        curation_action="propose",
        plan={"version": 1, "title": "Place notes", "steps": steps},
    )
    preview = writer_lease.invoke_command(
        command, vault, mode="curation", curation_action="preview", run_id=proposed["run_id"]
    )
    return proposed, preview


def test_a_split_previews_one_question_naming_each_child_and_leaves_the_fingerprint(
    vault: Path,
) -> None:
    steps = [
        _child_step("child-one", "Harbour fare notes", "project-alpha"),
        _child_step("child-two", "Island fare notes", "project-alpha"),
    ]

    proposed, preview = _propose_and_preview(vault, steps)

    asked = preview["ownership_question"]
    assert [(s["step_id"], s["project"]) for s in asked["steps"]] == [
        ("child-one", "project-alpha"),
        ("child-two", "project-alpha"),
    ]
    assert "child-one (project-alpha), child-two (project-alpha)" in asked["question"]
    assert semantic_authoring.AUTHORING_CONTRACT.routes["ownership_test"] in asked["question"]
    # The question is a reply field: the sealed plan the user confirms is the same plan.
    assert preview["plan_fingerprint"] == proposed["plan_fingerprint"]


def test_a_plan_without_note_placement_previews_no_question(vault: Path) -> None:
    _put(vault, PAGE, _text())
    edit = {
        "step_id": "retag",
        "kind": "edit",
        "args": {
            "path": PAGE,
            "why": "tidy the heading",
            "operation": {
                "kind": "replace_string",
                "old_string": "## Observations",
                "new_string": "## Rules",
                "expected_hash": content_hash((vault / PAGE).read_text(encoding="utf-8")),
            },
        },
    }

    _, preview = _propose_and_preview(vault, [edit])

    assert "ownership_question" not in preview


def test_a_move_that_assigns_a_project_is_named_with_that_project(vault: Path) -> None:
    _put(vault, PAGE, _text())
    move = {
        "step_id": "rehome",
        "kind": "move",
        "args": {
            "old_path": PAGE,
            "new_path": f"{FOLDER}/project-alpha/seasonal-fare-scouting.md",
            "fields": {"project": "project-alpha"},
        },
    }

    _, preview = _propose_and_preview(vault, [move])

    assert [(s["step_id"], s["project"]) for s in preview["ownership_question"]["steps"]] == [
        ("rehome", "project-alpha")
    ]
