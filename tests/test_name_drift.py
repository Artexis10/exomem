"""Name drift: pages whose file name no longer follows their title are found, listed and suggested.

Each test drives the real audit, the real read operation or the real edit command on a copy of
the fixture vault, and names the failure that only it catches. Pages are invented. The fixture
vault holds slug-named pages of its own, so each test compares against the vault as it stood
before the invented pages arrived.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from exomem import audit as audit_module
from exomem import due_state, name_drift, writer_lease
from exomem import vault as vault_module
from exomem.commands import PRODUCT_COMMANDS
from exomem.governance.principal import RequestPrincipal, owner_principal, request_scope

AREA = "Knowledge Base/Notes/Research/Harbour"


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


def _scan(root: Path) -> name_drift.Scan:
    pages = audit_module._parse_all(root / "Knowledge Base", root)
    return name_drift.scan(root, pages)


# One page per way a slug name and a title come apart, and the pages that must not be listed.
LONG_TITLE = ("Harbour dredging schedule " + "with seasonal tidal windows and crane allocations " * 6).strip()
DRIFTING = {
    f"{AREA}/celik-muller-uberblick.md": "Çelik Müller Überblick",
    f"{AREA}/mixed-case-title.md": "Mixed Case Title",
    f"{AREA}/plan-draft-2-v1.md": "Plan [draft] #2 | v^1",
    f"{AREA}/harbour-dredging-schedule.md": LONG_TITLE,
}
SOURCE = "Knowledge Base/Sources/Articles/2026-08-01-tide-almanac.md"
SESSION = "Knowledge Base/Sources/Sessions/2026-08-02-tide-planning.md"
UNTITLED = f"{AREA}/no-title-at-all.md"


def _invent(root: Path) -> None:
    for number, (rel, title) in enumerate(DRIFTING.items(), start=1):
        _put(root, rel, _page(title, number=number))
    _put(root, f"{AREA}/Already Right.md", _page("Already Right", number=10))
    _put(root, f"{AREA}/Already Right-2.md", _page("Already Right", number=11))
    _put(root, UNTITLED, _page(None, number=12))
    _put(root, SOURCE, "---\ntype: source\ntitle: Tide almanac\ncaptured: 2026-08-01\n---\n\n# Tide almanac\n")
    _put(
        root,
        SESSION,
        "---\ntype: source\nsource_type: session\ntitle: Tide planning session\ncaptured: 2026-08-02\n---\n\n# Tide planning\n",
    )


def test_the_scan_lists_exactly_the_pages_the_creation_path_would_name_differently(vault: Path) -> None:
    """Failure caught: a second naming rule inside the audit drifts from creation, so a page is
    listed that creation would keep, or a stale page is missed. Expected names come from creation."""
    before = {page.path for page in _scan(vault).drifted}
    _invent(vault)

    after = _scan(vault)

    new = {page.path: page for page in after.drifted if page.path not in before}
    assert set(new) == set(DRIFTING)
    for rel, title in DRIFTING.items():
        planned, _ = vault_module.resolve_filename_slug(title, None, vault_root=vault)
        assert new[rel].planned == f"{planned}.md"
        assert new[rel].current == Path(rel).name
        assert new[rel].title == title


def test_dated_pages_a_matching_page_and_its_collision_copy_are_never_listed(vault: Path) -> None:
    """Failure caught: renaming a Source or a recap loses its dated capture name; a page that
    already follows its title, or carries its collision number, is flagged and churned."""
    _invent(vault)

    listed = {page.path for page in _scan(vault).drifted}

    assert SOURCE not in listed and SESSION not in listed
    assert f"{AREA}/Already Right.md" not in listed
    assert f"{AREA}/Already Right-2.md" not in listed


def test_a_page_with_no_usable_title_is_counted_apart_and_never_proposed(vault: Path) -> None:
    """Failure caught: an untitled page is proposed for a rename to a made-up name."""
    _invent(vault)

    scan = _scan(vault)

    assert UNTITLED in {page.path for page in scan.untitled}
    assert UNTITLED not in {page.path for page in scan.drifted}
    assert UNTITLED not in {page.path for page in name_drift.next_batch(scan, limit=10_000)}


def test_the_audit_raises_one_vault_item_that_names_the_call_and_changes_with_the_set(vault: Path) -> None:
    """Failure caught: one item per page floods the queue, or a decision on the old set silences
    a different set."""
    _invent(vault)

    first = [f for f in audit_module.audit(vault, categories=["name_drift"]).findings if f.category == "name_drift"]
    (vault / f"{AREA}/mixed-case-title.md").rename(vault / f"{AREA}/Mixed Case Title.md")
    second = [f for f in audit_module.audit(vault, categories=["name_drift"]).findings if f.category == "name_drift"]

    assert len(first) == 1 and len(second) == 1
    assert "curation_action='relocate'" in first[0].detail
    assert "review_ref='exomem://review/" in first[0].detail
    assert first[0].meta["signal_version"] != second[0].meta["signal_version"]
    assert first[0].meta["drifted"] == second[0].meta["drifted"] + 1


def test_a_vault_with_no_drift_raises_no_item(vault: Path) -> None:
    """Failure caught: the family stays open at zero."""
    for page in _scan(vault).drifted:
        old = vault / page.path
        old.rename(old.with_name(page.planned))

    found = audit_module.audit(vault, categories=["name_drift"]).findings

    assert [f for f in found if f.category == "name_drift"] == []


def test_a_retitle_that_makes_the_name_drift_returns_the_rename_call_and_other_edits_return_nothing(
    vault: Path,
) -> None:
    """Failure caught: the user retitles a page and nobody renames it; or every edit nags."""
    _put(vault, f"{AREA}/Tide Table.md", _page("Tide Table", number=20))
    rel = f"{AREA}/Tide Table.md"

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
    assert suggestion["planned_name"] == f"{vault_module.resolve_filename_slug('Tide Table 2027', None, vault_root=vault)[0]}.md"
    assert "curation_action='relocate'" in suggestion["call"]
    assert "to_project" not in suggestion["call"]


TODAY = dt.date(2026, 8, 16)


def test_the_due_block_serves_one_name_drift_row_after_a_dated_obligation_and_a_refresh_lowers_it(
    vault: Path,
) -> None:
    """Failure caught: the agent is never told (the family is missing from the served block), the
    row outranks a dated obligation, or the count stays stale after pages are renamed."""
    _invent(vault)
    _put(
        vault,
        "Knowledge Base/Notes/Insights/old-question.md",
        "---\ntitle: Old question\ntype: insight\nstatus: active\n"
        "created: 2026-05-01\nupdated: 2026-05-01\n---\n\n"
        "## Open Question\n\n- id: q1\n\nDoes the projection survive a day boundary?\n",
    )
    due_state.reconcile(vault, today=TODAY)

    with request_scope(owner_principal(surface="mcp")):
        categories = [row["category"] for row in due_state.served_entries(vault, today=TODAY)]
        assert categories.count("name_drift") == 1
        assert categories.index("question_aging") < categories.index("name_drift")
        assert due_state.served(vault, today=TODAY)["outstanding"]["name_drift"] == 1
        for page in _scan(vault).drifted:
            old = vault / page.path
            old.rename(old.with_name(page.planned))
        due_state.refresh_name_drift(vault, today=TODAY)
        after = [row["category"] for row in due_state.served_entries(vault, today=TODAY)]
    assert "name_drift" not in after


def test_a_restricted_caller_is_not_served_the_vault_wide_row(vault: Path) -> None:
    """Failure caught: the stored count covers every page, so a caller that may not see some of
    them would learn how many pages the vault holds with stale names."""
    _invent(vault)
    due_state.reconcile(vault, today=TODAY)

    with request_scope(RequestPrincipal(audience_id="client-a", surface="mcp")):
        categories = [row["category"] for row in due_state.served_entries(vault, today=TODAY)]

    assert "name_drift" not in categories


def test_the_listing_pages_through_the_drifted_pages_and_marks_the_untitled_one(vault: Path) -> None:
    """Failure caught: a listing that repeats or skips a row across pages, proposes a rename for a
    page with no usable title, or reports a total that is not the audit's count."""
    _invent(vault)
    expected = _scan(vault)
    rows = []
    cursor = None
    while True:
        page = name_drift.listing(vault, cursor=cursor, limit=7)
        rows.extend(page["pages"])
        cursor = page.get("next_cursor")
        if cursor is None:
            break

    paths = [row["ref"] for row in rows]
    assert len(paths) == len(set(paths)) == len(expected.drifted) + len(expected.untitled)
    assert page["total_drifted"] == len(expected.drifted)
    untitled = [row for row in rows if row["state"] == "no_usable_title"]
    assert Path(UNTITLED).name in [row["current_name"] for row in untitled]
    assert all("planned_name" not in row for row in untitled)
    assert "curation_action='relocate'" in page["rename_next_batch"]


def test_a_scan_the_budget_stops_reports_no_total(vault: Path, monkeypatch) -> None:
    """Failure caught: an unfinished scan reads as zero drift."""
    from exomem import audit, request_budget

    def stopped(*_args, **_kwargs):
        raise request_budget.ReadBudgetExhausted()

    monkeypatch.setattr(audit, "_parse_all", stopped)

    answer = name_drift.listing(vault)

    assert answer["complete"] is False
    assert answer["total_drifted"] is None


def _maintain(vault: Path, action: str, **arguments: object) -> dict:
    return writer_lease.invoke_command(
        _command("maintain_memory"), vault, mode="curation", curation_action=action, **arguments
    )


def _item_ref(vault: Path) -> str:
    due_state.refresh_name_drift(vault)
    (found,) = [f for f in audit_module.audit(vault, categories=["name_drift"]).findings if f.category == "name_drift"]
    return found.meta["review_ref"]


def _run_to_end(vault: Path, started: dict, *, run_id: str, plan_id: str, done: str) -> dict:
    result = started
    while result["phase"] == "executing" or result["phase"] == "compensating":
        result = _maintain(vault, "resume", run_id=run_id, plan_id=plan_id)
    assert result["phase"] == done, result
    return result


def test_one_review_ref_renames_the_batch_to_its_planned_names_and_one_approval_undoes_it(vault: Path) -> None:
    """Failure caught: the call that the review item carries renames only the page it is anchored on,
    renames to a name the drift rule did not plan, or cannot be undone with one approval."""
    _invent(vault)
    before = _scan(vault)
    batch = {page.path: page.planned for page in name_drift.next_batch(before)}
    snapshot = {rel: (vault / rel).read_bytes() for rel in batch}

    proposed = _maintain(vault, "relocate", review_ref=_item_ref(vault))

    assert proposed["sealed"] is True
    assert proposed["summary"]["moves"]["count"] == len(batch)
    assert "deferred_to_next_batch" not in proposed["summary"]
    applied = _maintain(
        vault,
        "apply",
        run_id=proposed["run_id"],
        plan_id=proposed["plan_id"],
        expected_plan_fingerprint=proposed["plan_fingerprint"],
        why="Approved rename batch.",
    )
    _run_to_end(vault, applied, run_id=proposed["run_id"], plan_id=proposed["plan_id"], done="completed")

    for rel, planned in batch.items():
        assert not (vault / rel).exists()
        assert (vault / rel).with_name(planned).exists(), planned
    assert not set(batch) & {page.path for page in _scan(vault).drifted}

    undo = _maintain(vault, "propose-compensation", run_id=proposed["run_id"])
    undone = _maintain(
        vault,
        "apply-compensation",
        run_id=proposed["run_id"],
        plan_id=undo["plan_id"],
        expected_plan_fingerprint=undo["plan_fingerprint"],
        why="Undo the rename batch.",
    )
    _run_to_end(vault, undone, run_id=proposed["run_id"], plan_id=undo["plan_id"], done="compensated")

    assert {rel: (vault / rel).read_bytes() for rel in batch} == snapshot


def test_a_batch_too_large_to_seal_shrinks_and_names_the_pages_left_for_the_next(vault: Path, monkeypatch) -> None:
    """Failure caught: the server-chosen batch exceeds the sealed-size bound and the whole backlog
    refuses, with no way for the agent to make the batch smaller."""
    from exomem import curation

    _invent(vault)
    batch = name_drift.next_batch(_scan(vault))
    whole = _maintain(vault, "relocate", review_ref=_item_ref(vault))
    sealed = len(json.dumps(curation.CurationStore(vault).load_plan(whole["run_id"])).encode())
    monkeypatch.setattr(curation, "MAX_SEALED_BYTES", int(sealed * 0.75))

    proposed = _maintain(vault, "relocate", review_ref=_item_ref(vault))

    moved = proposed["summary"]["moves"]["count"]
    assert proposed["sealed"] is True
    assert 0 < moved < len(batch)
    assert proposed["summary"]["deferred_to_next_batch"]["count"] == len(batch) - moved
