"""Name drift: pages whose file name no longer follows their title are found, renamed and closed.

Each test drives the real audit, due-state block, curation call or edit command on a copy of the
fixture vault, and names the failure that only it catches. Pages are invented. The fixture vault
holds slug-named pages of its own, and the backlog test renames them with the invented ones.
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
from pathlib import Path

import pytest

from exomem import audit as audit_module
from exomem import due_state, name_drift, writer_lease
from exomem.commands import PRODUCT_COMMANDS, op_attention
from exomem.governance.principal import RequestPrincipal, owner_principal, request_scope

AREA = "Knowledge Base/Notes/Research/Harbour"
PEOPLE = "Knowledge Base/Entities/People"
REELS = "Knowledge Base/Notes/Productions/Reels"


def _page(title: str | None, *, page_type: str = "research-note", number: int = 1) -> str:
    title_line = f"title: {json.dumps(title)}\n" if title is not None else ""
    return (
        "---\n"
        f"{title_line}"
        f"type: {page_type}\n"
        "project: harbour\n"
        "status: active\n"
        f"exomem_id: 00000000-0000-4000-8000-{number:012d}\n"
        "updated: 2026-08-15\n"
        "---\n\n"
        "## Observations\n\n- [finding] The tide table was revised. #harbour\n"
    )


def _put(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _command(name: str):
    return next(command for command in PRODUCT_COMMANDS if command.name == name)


def _maintain(vault: Path, action: str, **arguments: object) -> dict:
    return writer_lease.invoke_command(
        _command("maintain_memory"), vault, mode="curation", curation_action=action, **arguments
    )


def _run_to_end(vault: Path, started: dict, *, run_id: str, plan_id: str, done: str) -> dict:
    result = started
    while result["phase"] in ("executing", "compensating"):
        result = _maintain(vault, "resume", run_id=run_id, plan_id=plan_id)
    assert result["phase"] == done, result
    return result


def _findings(vault: Path) -> list:
    report = audit_module.audit(vault, categories=["name_drift"])
    return [finding for finding in report.findings if finding.category == "name_drift"]


def _name_drift_row(vault: Path) -> dict | None:
    rows = [row for row in due_state.served_entries(vault) if row["category"] == "name_drift"]
    assert len(rows) <= 1
    return rows[0] if rows else None


# Each page that a rename-only relocation renames, with the path it must end at. The expected
# paths are written out rather than asked of the shared rule, so a wrong rule fails here.
RENAMED = {
    f"{AREA}/outer-tide-table.md": f"{AREA}/Tide table for the outer harbour.md",
    **{f"{AREA}/harbour-note-{n}.md": f"{AREA}/Harbour note {n}.md" for n in range(1, 5)},
    # An entity is renamed where it stands; its title is its heading.
    f"{PEOPLE}/ada-byron.md": f"{PEOPLE}/Ada Byron.md",
    # A dated type keeps the date prefix of its current name.
    f"{REELS}/2026-07-harbour-reel.md": f"{REELS}/2026-07-Harbour reel batch.md",
}
# Pages that no relocation renames: a name that differs only in case, a page with no usable
# title, a page that holds its numbered name, and the dated Sources and recaps.
KEPT = (
    f"{AREA}/crane rota.md",
    f"{AREA}/no-title-at-all.md",
    f"{AREA}/Already Right.md",
    f"{AREA}/Already Right-2.md",
    "Knowledge Base/Sources/Articles/2026-08-01-tide-almanac.md",
    "Knowledge Base/Sources/Sessions/2026-08-02-tide-planning.md",
)


def _invent(root: Path) -> None:
    _put(root, f"{AREA}/outer-tide-table.md", _page("Tide table for the outer harbour", number=1))
    _put(
        root,
        f"{PEOPLE}/ada-byron.md",
        "---\ntype: entity\nentity_type: person\nstatus: active\ncreated: 2026-08-01\n"
        "updated: 2026-08-01\n---\n\n# Ada Byron\n\n## Summary\n\nHarbour pilot.\n",
    )
    _put(
        root,
        f"{REELS}/2026-07-harbour-reel.md",
        _page("Harbour reel batch", page_type="production-log", number=6).replace(
            "status: active\n", "medium: reels\nstatus: recorded\n"
        ),
    )
    for n in range(1, 5):
        _put(root, f"{AREA}/harbour-note-{n}.md", _page(f"Harbour note {n}", number=30 + n))
    _put(root, f"{AREA}/crane rota.md", _page("Crane Rota", number=2))
    _put(root, f"{AREA}/no-title-at-all.md", _page(None, number=3))
    _put(root, f"{AREA}/Already Right.md", _page("Already Right", number=4))
    _put(root, f"{AREA}/Already Right-2.md", _page("Already Right", number=5))
    _put(
        root,
        "Knowledge Base/Sources/Articles/2026-08-01-tide-almanac.md",
        "---\ntype: source\ntitle: Tide almanac\ncaptured: 2026-08-01\n---\n\n# Tide almanac\n",
    )
    _put(
        root,
        "Knowledge Base/Sources/Sessions/2026-08-02-tide-planning.md",
        "---\ntype: source\nsource_type: session\ntitle: Tide planning session\n"
        "captured: 2026-08-02\n"
        "---\n\n# Tide planning\n",
    )


@pytest.fixture
def harbour(vault: Path) -> Path:
    """The fixture vault with the invented pages in place of its own compiled notes.

    Most of those notes lack the semantic unit that the move contract requires of a page it
    writes, so a rename batch would refuse them; the backlog here is the invented pages alone.
    """
    shutil.rmtree(vault / "Knowledge Base" / "Notes")
    _invent(vault)
    return vault


def test_the_items_call_renames_the_backlog_batch_by_batch_until_the_item_closes(
    harbour: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failure caught: the item's call renames a page the rule did not plan, or a page that must
    keep its name; the item's identity moves with its own renames, so the ref the agent holds stops
    working mid-backlog; a finished batch leaves the row counting renamed pages; the backlog never
    closes; or one batch cannot be undone exactly."""
    # The preview size is the operator's cadence parameter. A small one makes this vault's
    # backlog take several batches, which is the loop under test.
    monkeypatch.setattr(name_drift, "BATCH_PAGES", 3)
    vault = harbour
    kept = {rel: (vault / rel).read_bytes() for rel in KEPT}
    due_state.reconcile(vault)
    batches: list[tuple[dict, dict[str, bytes]]] = []

    with request_scope(owner_principal(surface="mcp")):
        first = _name_drift_row(vault)
        assert first is not None
        (found,) = _findings(vault)
        assert f"review_ref='{first['ref']}'" in found.meta["call"]
        for _ in range(12):
            row = _name_drift_row(vault)
            if row is None:
                break
            # The ref the agent was first given still names the item, and resolves to the batch
            # that is next now, not to the pages an earlier batch renamed.
            assert row["ref"] == first["ref"]
            batch = name_drift.pages_for_ref(vault, first["ref"])
            assert batch and len(batch) <= 3
            assert not set(batch) & {rel for _, done in batches for rel in done}
            snapshot = {rel: (vault / rel).read_bytes() for rel in batch}
            proposed = _maintain(vault, "relocate", review_ref=first["ref"])
            assert proposed["summary"]["moves"]["count"] == len(batch)
            applied = _maintain(
                vault,
                "apply",
                run_id=proposed["run_id"],
                plan_id=proposed["plan_id"],
                expected_plan_fingerprint=proposed["plan_fingerprint"],
                why="Approved rename batch.",
            )
            _run_to_end(
                vault, applied, run_id=proposed["run_id"], plan_id=proposed["plan_id"],
                done="completed",
            )
            assert not any((vault / rel).exists() for rel in batch)
            batches.append((proposed, snapshot))
        else:
            pytest.fail("the name-drift backlog did not close")

    assert len(batches) >= 3
    assert name_drift._corpus_scan(vault).drifted == ()
    for before, after in RENAMED.items():
        assert not (vault / before).exists()
        assert (vault / after).is_file(), after
    assert {rel: (vault / rel).read_bytes() for rel in KEPT} == kept

    proposed, snapshot = batches[-1]
    undo = _maintain(vault, "propose-compensation", run_id=proposed["run_id"])
    undone = _maintain(
        vault,
        "apply-compensation",
        run_id=proposed["run_id"],
        plan_id=undo["plan_id"],
        expected_plan_fingerprint=undo["plan_fingerprint"],
        why="Undo the last rename batch.",
    )
    _run_to_end(
        vault, undone, run_id=proposed["run_id"], plan_id=undo["plan_id"], done="compensated"
    )
    assert {rel: (vault / rel).read_bytes() for rel in snapshot} == snapshot


def test_the_review_listing_serves_the_item_from_its_stored_row_without_a_scan(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failure caught: every review listing pays a scan of every page for one housekeeping row."""
    _invent(vault)
    due_state.reconcile(vault)

    def scanned(*_args, **_kwargs):
        raise AssertionError("the review listing scanned the vault")

    monkeypatch.setattr(name_drift, "scan", scanned)
    with request_scope(owner_principal(surface="mcp")):
        items = op_attention(vault, limit=0)["items"]

    (item,) = [item for item in items if "name_drift" in item["categories"]]
    assert "curation_action='relocate'" in item["reasons"][0]["detail"]


def test_the_one_item_changes_its_signal_with_the_drifted_set(vault: Path) -> None:
    """Failure caught: one item per page floods the queue, or a decision on the old set silences
    a different set."""
    _invent(vault)

    first = _findings(vault)
    area = vault / AREA
    (area / "outer-tide-table.md").rename(area / "Tide table for the outer harbour.md")
    second = _findings(vault)

    assert len(first) == 1 and len(second) == 1
    assert first[0].path == second[0].path
    assert first[0].meta["signal_version"] != second[0].meta["signal_version"]
    assert first[0].meta["drifted"] == second[0].meta["drifted"] + 1


def test_a_retitle_that_makes_the_name_drift_returns_the_rename_call_and_other_edits_return_nothing(
    vault: Path,
) -> None:
    """Failure caught: the user retitles a page and nobody renames it; or every edit nags."""
    rel = f"{AREA}/Tide Table.md"
    _put(vault, rel, _page("Tide Table", number=20))

    def patch(field: str, value) -> dict:
        return writer_lease.invoke_command(
            _command("edit_memory"),
            vault,
            path=rel,
            operation={"kind": "patch_frontmatter", "field": field, "value": value},
            why="test edit",
            response_detail="full",
        )

    untouched = patch("status", "active")
    retitled = patch("title", "Tide Table 2027")

    assert not untouched.get("structure_suggestion")
    suggestion = retitled["structure_suggestion"]
    assert suggestion["kind"] == "rename"
    assert suggestion["planned_name"] == "Tide Table 2027.md"
    assert "curation_action='relocate'" in suggestion["call"]
    assert "to_project" not in suggestion["call"]


def test_the_name_drift_row_is_served_after_a_dated_obligation(vault: Path) -> None:
    """Failure caught: housekeeping outranks an obligation a person dated."""
    today = dt.date(2026, 8, 16)
    _invent(vault)
    _put(
        vault,
        "Knowledge Base/Notes/Insights/old-question.md",
        "---\ntitle: Old question\ntype: insight\nstatus: active\n"
        "created: 2026-05-01\nupdated: 2026-05-01\n---\n\n"
        "## Open Question\n\n- id: q1\n\nDoes the projection survive a day boundary?\n",
    )
    due_state.reconcile(vault, today=today)

    with request_scope(owner_principal(surface="mcp")):
        categories = [row["category"] for row in due_state.served_entries(vault, today=today)]

    assert categories.count("name_drift") == 1
    assert categories.index("question_aging") < categories.index("name_drift")


def test_a_restricted_caller_is_not_served_the_vault_wide_row(vault: Path) -> None:
    """Failure caught: the stored count covers every page, so a caller that may not see some of
    them would learn how many pages the vault holds with stale names."""
    _invent(vault)
    due_state.reconcile(vault)

    with request_scope(RequestPrincipal(audience_id="client-a", surface="mcp")):
        categories = [row["category"] for row in due_state.served_entries(vault)]

    assert "name_drift" not in categories


def test_the_listing_pages_through_the_drifted_pages_and_marks_the_untitled_one(
    vault: Path,
) -> None:
    """Failure caught: a listing that repeats or skips a row across pages, proposes a rename for a
    page with no usable title, or reports a total that is not the audit's count."""
    _invent(vault)
    expected = name_drift._corpus_scan(vault)
    rows = []
    cursor = None
    while True:
        page = name_drift.listing(vault, cursor=cursor, limit=7)
        rows.extend(page["pages"])
        cursor = page.get("next_cursor")
        if cursor is None:
            break

    refs = [row["ref"] for row in rows]
    assert len(refs) == len(set(refs)) == len(expected.drifted) + len(expected.untitled)
    assert page["total_drifted"] == len(expected.drifted)
    untitled = [row for row in rows if row["state"] == "no_usable_title"]
    assert [row["current_name"] for row in untitled] == ["no-title-at-all.md"]
    assert all("planned_name" not in row for row in untitled)
    assert "curation_action='relocate'" in page["rename_next_batch"]


def test_a_scan_the_budget_stops_reports_no_total(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failure caught: an unfinished scan reads as zero drift."""
    from exomem import request_budget

    def stopped(*_args, **_kwargs):
        raise request_budget.ReadBudgetExhausted()

    monkeypatch.setattr(audit_module, "_parse_all", stopped)

    answer = name_drift.listing(vault)

    assert answer["complete"] is False
    assert answer["total_drifted"] is None


def test_a_batch_too_large_to_seal_shrinks_and_names_the_pages_left_for_the_next(
    harbour: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failure caught: the server-chosen batch exceeds the sealed-size bound and the whole backlog
    refuses, with no way for the agent to make the batch smaller."""
    from exomem import curation

    vault = harbour
    ref = name_drift.item_ref(vault)
    batch = name_drift.pages_for_review(vault)
    whole = _maintain(vault, "relocate", review_ref=ref)
    sealed = len(json.dumps(curation.CurationStore(vault).load_plan(whole["run_id"])).encode())
    monkeypatch.setattr(curation, "MAX_SEALED_BYTES", int(sealed * 0.75))

    proposed = _maintain(vault, "relocate", review_ref=ref)

    moved = proposed["summary"]["moves"]["count"]
    assert proposed["sealed"] is True
    assert 0 < moved < len(batch)
    assert proposed["summary"]["deferred_to_next_batch"]["count"] == len(batch) - moved
