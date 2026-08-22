"""The maintained due-state projection: invalidation, day boundaries, recovery, egress.

The projection exists because neither of the two obvious designs works. A per-call
computation cannot ride on a mutating response — the write that carries it is the write
that invalidates it — and a cache of the attention summary is a cache of something that is
rebuilt on every call. So the projection is MAINTAINED: an incremental per-write delta for
the categories a written page can participate in, a day-boundary re-bucket that compares
stored dates against today (no parse, no audit), reconcile as the healer after out-of-band
edits, and full recomputation as the recovery path.

Two invariants get adversarial tests here rather than incidental ones.

*Day boundaries.* A `check_by` passes at midnight with no write and no generation token can
see it. The projection therefore persists, per category and per page, the items already due
AND the candidates that are not yet due together with the date each becomes due, so
promotion is a date comparison rather than a rescan.

*Egress before counting.* A count is an aggregate and the governance plane's silence rule
extends to aggregates. A withheld item contributes nothing to any count, reference list or
ordering, and the served view is byte-identical to the same vault with the item absent —
`files_direct: 1` beside `sample_names: []` is a stronger oracle than the list it replaced,
and a category count beside an empty `top` would be exactly that mistake again.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from exomem import due_state as due_state_module
from exomem import find as find_module
from exomem.governance import egress as egress_module
from exomem.governance.principal import RequestPrincipal, request_scope

TODAY = dt.date(2026, 8, 16)
TOMORROW = dt.date(2026, 8, 17)

INSIGHTS = "Knowledge Base/Notes/Insights"
RESTRICTED = "Knowledge Base/Notes/Patterns"

SCOPE_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
RULE_ID = "01ARZ3NDEKTSV4RRFFQ69G5FB0"
EXTERNAL = "external"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _write(vault: Path, rel: str, text: str) -> str:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    find_module.clear_cache()
    return rel


def _prediction(
    vault: Path,
    slug: str,
    *,
    check_by: str,
    folder: str = INSIGHTS,
    verdict: str | None = None,
    anchor: str = "p1",
) -> str:
    head = (
        "---\n"
        f"title: {slug}\n"
        "type: insight\n"
        "status: active\n"
        "created: 2026-01-01\n"
        "updated: 2026-01-01\n"
        "---\n\n"
    )
    rows = [f"- id: {anchor}", f"- check_by: {check_by}"]
    if verdict is not None:
        rows.append(f"- verdict: {verdict}")
    block = "## Prediction\n\n" + "\n".join(rows) + "\n\nThe backlog clears within a week.\n"
    return _write(vault, f"{folder}/{slug}.md", head + block)


EXPERIMENTS = "Knowledge Base/Notes/Experiments/Infrastructure"


def _question_page(vault: Path, slug: str, *, created: str) -> str:
    """A long-unanswered question — a `question_aging` item, opt-in category."""
    return _write(
        vault,
        f"{INSIGHTS}/{slug}.md",
        f"---\ntitle: {slug}\ntype: insight\nstatus: active\n"
        f"created: {created}\nupdated: {created}\n---\n\n"
        "## Open Question\n\n- id: q1\n\n"
        "Does the projection survive a day boundary with no write?\n",
    )


def _experiment(vault: Path, slug: str, *, started: str, duration: str) -> str:
    """An experiment past its declared window with no outcome — opt-in category."""
    return _write(
        vault,
        f"{EXPERIMENTS}/{slug}.md",
        f"---\ntitle: {slug}\ntype: experiment\ndomain: infrastructure\n"
        f"status: active\ncreated: 2025-01-01\nupdated: 2025-01-01\n"
        f'started: {started}\nduration: "{duration}"\nn: 1\n---\n\n'
        "## Hypothesis\n\nIt will work.\n",
    )


def _gov_dir(vault: Path) -> Path:
    return vault / "Knowledge Base" / "_Governance"


def _restrict_patterns(vault: Path, *, ceiling: int = 0) -> None:
    """A policy that withholds `Notes/Patterns/**` from the `external` audience."""
    scope = _gov_dir(vault) / "scopes" / "patterns.yaml"
    scope.parent.mkdir(parents=True, exist_ok=True)
    scope.write_text(
        f'governance_version: 1\nid: {SCOPE_ID}\nname: Patterns\npaths: ["Notes/Patterns/**"]\n',
        encoding="utf-8",
    )
    rule = _gov_dir(vault) / "rules" / "patterns-external.yaml"
    rule.parent.mkdir(parents=True, exist_ok=True)
    rule.write_text(
        f'governance_version: 1\nid: {RULE_ID}\nscope_ids: ["{SCOPE_ID}"]\n'
        f"audience: {EXTERNAL}\nceiling: {ceiling}\n",
        encoding="utf-8",
    )
    _reset_governance_caches()


def _reset_governance_caches() -> None:
    from exomem.governance import membership, policy

    policy._CACHE.clear()
    membership.clear_memo()
    egress_module.clear_decision_memo()
    find_module.clear_cache()


@pytest.fixture(autouse=True)
def _clean_state():
    due_state_module.reset_emission_state()
    _reset_governance_caches()
    yield
    due_state_module.reset_emission_state()
    _reset_governance_caches()


def _served(vault: Path, *, today: dt.date = TODAY) -> dict | None:
    return due_state_module.served(vault, today=today)


# ==========================================================================
# recompute + persistence
# ==========================================================================


def test_a_full_recompute_persists_state_beside_the_review_state(vault: Path) -> None:
    _prediction(vault, "due-one", check_by="2026-08-01")

    due_state_module.reconcile(vault, today=TODAY)

    path = due_state_module.state_path(vault)
    assert path.exists()
    assert path.parent == (vault / "Knowledge Base")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["version"] == due_state_module.SCHEMA_VERSION
    assert payload["computed_on"] == TODAY.isoformat()


def test_the_served_block_reports_totals_and_bounded_top_references(vault: Path) -> None:
    _prediction(vault, "due-one", check_by="2026-08-01")
    _prediction(vault, "due-two", check_by="2026-07-01")
    due_state_module.reconcile(vault, today=TODAY)

    block = _served(vault)

    assert block is not None
    assert block["total"] == 2
    assert block["categories"] == {"prediction_window": 2}
    assert len(block["top"]) == 2
    assert len(block["top"]) <= due_state_module.TOP_LIMIT
    first = block["top"][0]
    assert set(first) == {"category", "ref", "due_since"}
    assert first["category"] == "prediction_window"
    assert first["ref"].startswith("exomem://review/")
    # Most-overdue first: the older check date leads.
    assert [row["due_since"] for row in block["top"]] == ["2026-07-01", "2026-08-01"]


def test_the_top_list_is_bounded(vault: Path) -> None:
    for index in range(due_state_module.TOP_LIMIT + 3):
        _prediction(vault, f"due-{index}", check_by="2026-08-01", anchor=f"p{index}")
    due_state_module.reconcile(vault, today=TODAY)

    block = _served(vault)

    assert block is not None
    assert block["total"] == due_state_module.TOP_LIMIT + 3
    assert len(block["top"]) == due_state_module.TOP_LIMIT


def test_an_empty_projection_serves_nothing_at_all(vault: Path) -> None:
    """Absent, never null and never an empty block — the advisory posture."""
    due_state_module.reconcile(vault, today=TODAY)

    assert _served(vault) is None


# ==========================================================================
# incremental delta on write
# ==========================================================================


def test_a_write_updates_only_the_written_pages_entries(vault: Path) -> None:
    _prediction(vault, "first", check_by="2026-08-01")
    due_state_module.reconcile(vault, today=TODAY)
    assert _served(vault)["total"] == 1

    rel = _prediction(vault, "second", check_by="2026-07-01")
    due_state_module.apply_write_delta(vault, rel, today=TODAY)

    block = _served(vault)
    assert block["total"] == 2
    assert block["categories"]["prediction_window"] == 2


def test_a_delta_removes_the_written_pages_resolved_item(vault: Path) -> None:
    rel = _prediction(vault, "one", check_by="2026-08-01")
    due_state_module.reconcile(vault, today=TODAY)
    assert _served(vault)["total"] == 1

    _prediction(vault, "one", check_by="2026-08-01", verdict="confirmed")
    due_state_module.apply_write_delta(vault, rel, today=TODAY)

    assert _served(vault) is None


def _dangling(vault: Path, slug: str, *, target: str, status: str = "superseded") -> str:
    """A page whose `superseded_by` points nowhere — the page-local defect."""
    return _write(
        vault,
        f"{INSIGHTS}/{slug}.md",
        f"---\ntitle: {slug}\ntype: insight\nstatus: {status}\n"
        f"created: 2026-01-01\nupdated: 2026-06-01\n"
        f'superseded_by: "[[{target}]]"\n---\n\n# {slug}\n\nBody.\n',
    )


def test_a_write_that_repairs_a_dangling_pointer_stops_counting_immediately(
    vault: Path,
) -> None:
    """The page-local half of `supersession_integrity` settles on the write.

    It is the only `warn` category the counter carries and the one a user acts on
    at once, so continuing to report it until the next reconcile is the worst
    behaviour available: the counter would nag about the thing just fixed.
    """
    real = _prediction(vault, "successor", check_by="2026-12-01")
    rel = _dangling(vault, "old", target=f"{INSIGHTS}/nowhere")
    due_state_module.reconcile(vault, today=TODAY)
    assert _served(vault)["categories"]["supersession_integrity"] == 1

    _dangling(vault, "old", target=real.removesuffix(".md"))
    due_state_module.apply_write_delta(vault, rel, today=TODAY)

    served = _served(vault)
    assert served is None or "supersession_integrity" not in served["categories"]


def test_a_write_does_not_delete_a_multi_headed_chain_the_full_pass_found(
    vault: Path,
) -> None:
    """The chain-scoped half is NOT the write's to settle.

    Whether writing one page forks a chain depends on what other pages point at
    its predecessor. A delta that recomputed the whole category from the written
    page alone would erase a fork it cannot see — so the stored multi-head entry
    must survive a write that touches the same page.
    """
    def _head(slug: str, *, updated: str, body: str) -> str:
        return _write(
            vault,
            f"{INSIGHTS}/{slug}.md",
            f"---\ntitle: {slug}\ntype: insight\nstatus: active\ncreated: 2026-01-01\n"
            f'updated: {updated}\nsupersedes: "[[{INSIGHTS}/root]]"\n---\n\n# {slug}\n\n{body}\n',
        )

    _dangling(vault, "root", target=f"{INSIGHTS}/head-a")
    _head("head-a", updated="2026-06-01", body="Body.")
    _head("head-b", updated="2026-06-01", body="Body.")
    due_state_module.reconcile(vault, today=TODAY)
    before = _served(vault)
    assert before is not None
    forks = before["categories"].get("supersession_integrity", 0)
    assert forks >= 1, "the fixture must actually fork, or this test covers nothing"

    # The finding is anchored on the first member by path, so writing THAT page is
    # the case that matters: a delta which recomputed the category from the written
    # page alone would drop the fork entirely. Writing a non-anchor page proves
    # nothing, because the stored entry lives under a different key.
    anchor = f"{INSIGHTS}/head-a.md"
    stored = due_state_module.load(vault)["categories"]["supersession_integrity"]
    assert anchor in stored, (
        "the fork is not anchored where this test assumes; re-read the anchor rule "
        f"in audit._multi_headed_chain_findings (stored under {sorted(stored)})"
    )

    rel = _head("head-a", updated="2026-08-16", body="More body.")
    due_state_module.apply_write_delta(vault, rel, today=TODAY)

    after = _served(vault)
    assert after is not None
    assert after["categories"].get("supersession_integrity", 0) == forks


def test_a_delta_does_not_rerun_the_full_audit(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The load-bearing bound: a write must never pay for a vault-wide audit.

    `audit.audit` walks every page. If the delta reaches it, the write-latency
    gates are decided by corpus size, which is precisely the failure the
    maintained projection exists to avoid.
    """
    rel = _prediction(vault, "one", check_by="2026-08-01")
    due_state_module.reconcile(vault, today=TODAY)

    from exomem import audit as audit_module

    def _forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("the per-write delta ran a full audit pass")

    monkeypatch.setattr(audit_module, "audit", _forbidden)
    due_state_module.apply_write_delta(vault, rel, today=TODAY)


def test_a_delta_without_persisted_state_does_not_recompute(vault: Path) -> None:
    """Missing state must not turn a write into a full recompute inside the lock.

    The recovery path is real, but it belongs to a read surface outside the
    mutation critical section — never to the write itself.
    """
    rel = _prediction(vault, "one", check_by="2026-08-01")
    assert not due_state_module.state_path(vault).exists()

    assert due_state_module.apply_write_delta(vault, rel, today=TODAY) is None
    assert not due_state_module.state_path(vault).exists()


# ==========================================================================
# day-boundary re-bucketing
# ==========================================================================


def test_midnight_promotes_a_stored_candidate_with_no_write(vault: Path) -> None:
    """The motivating case: nothing happens, and something becomes due anyway."""
    _prediction(vault, "tomorrow", check_by=TOMORROW.isoformat())
    due_state_module.reconcile(vault, today=TODAY)

    assert _served(vault, today=TODAY) is None

    before = due_state_module.state_path(vault).read_bytes()
    block = _served(vault, today=TOMORROW)

    assert block is not None
    assert block["total"] == 1
    assert block["top"][0]["due_since"] == TOMORROW.isoformat()
    # Re-bucketing is a comparison against stored dates, not a rescan: serving
    # must not rewrite the projection.
    assert due_state_module.state_path(vault).read_bytes() == before


def test_the_pending_candidate_is_persisted_with_the_date_it_becomes_due(
    vault: Path,
) -> None:
    _prediction(vault, "tomorrow", check_by=TOMORROW.isoformat())
    due_state_module.reconcile(vault, today=TODAY)

    payload = json.loads(due_state_module.state_path(vault).read_text(encoding="utf-8"))
    pages = payload["categories"]["prediction_window"]
    entry = next(iter(pages.values()))
    assert entry["open"] == []
    assert len(entry["pending"]) == 1
    assert entry["pending"][0]["due_on"] == TOMORROW.isoformat()


def test_promotion_needs_no_audit_pass(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prediction(vault, "tomorrow", check_by=TOMORROW.isoformat())
    due_state_module.reconcile(vault, today=TODAY)

    from exomem import audit as audit_module

    def _forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("day-boundary re-bucketing ran an audit pass")

    monkeypatch.setattr(audit_module, "audit", _forbidden)
    block = _served(vault, today=TOMORROW)
    assert block is not None and block["total"] == 1


# ==========================================================================
# reconcile healing + recovery
# ==========================================================================


def test_reconcile_heals_an_out_of_band_edit(vault: Path) -> None:
    """A file changed by Obsidian, git, or a human — no write hook fired.

    The edit PUSHES THE CHECK DATE OUT rather than deleting the page: the author
    decides in Obsidian that the prediction needs another quarter. Serving has no
    way to notice that from stored state, so the projection keeps claiming the
    item until the healer runs — which is exactly the staleness `reconcile` exists
    for. (A deleted page is a different story and is handled at serve time; see
    the vanished-path test below.)
    """
    _prediction(vault, "one", check_by="2026-08-01")
    due_state_module.reconcile(vault, today=TODAY)
    assert _served(vault)["total"] == 1

    _prediction(vault, "one", check_by="2026-12-01")

    # The stale projection still claims the item until the healer runs.
    assert _served(vault)["total"] == 1

    due_state_module.reconcile(vault, today=TODAY)
    assert _served(vault) is None


def test_a_page_deleted_out_of_band_stops_counting_before_reconcile(
    vault: Path,
) -> None:
    """Reconcile is the healer, but a vanished page must not be counted meanwhile.

    A deleted page is the one staleness serving CAN see for itself, and it is the
    shape a user notices: they delete a note and the counter keeps insisting the
    vault owes something about it. Every category is dropped, not just the ones a
    write can delta.
    """
    _prediction(vault, "one", check_by="2026-08-01")
    _prediction(vault, "two", check_by="2026-08-02")
    due_state_module.reconcile(vault, today=TODAY)
    assert _served(vault)["total"] == 2

    (vault / f"{INSIGHTS}/one.md").unlink()
    (vault / f"{INSIGHTS}/two.md").unlink()
    find_module.clear_cache()

    assert _served(vault) is None


def test_a_vault_that_owes_nothing_never_builds_the_release_filter(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty projection has no input for the egress rule, so it skips it.

    This is not a relaxation of "egress before counting" — with no stored entry
    the served view is the empty list under any filter, so there is nothing a
    filter could change. It matters because building the release filter is
    governance-proportional work paid on every recall and every bootstrap, and a
    vault that owes nothing is the common case. Leaving it in showed up as
    measurable overhead on the governed read path.

    Pinned on the CALL rather than on a duration, so it is not a timing test.
    """
    from exomem.governance import egress as egress_module

    calls: list[object] = []
    real = egress_module.release_walk_filter
    monkeypatch.setattr(
        egress_module,
        "release_walk_filter",
        lambda *a, **k: (calls.append(1), real(*a, **k))[1],
    )

    due_state_module.reconcile(vault, today=TODAY)
    assert _served(vault) is None
    assert calls == [], "an empty projection consulted the release plane anyway"

    # And the moment it owes something, the filter is back on the path.
    _prediction(vault, "one", check_by="2026-08-01")
    due_state_module.reconcile(vault, today=TODAY)
    assert _served(vault)["total"] == 1
    assert calls, "a non-empty projection must still be filtered before counting"


def test_removing_the_vanished_path_drop_fails_this_module(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mechanism removal: with the existence check gone, the deleted page is back."""
    _prediction(vault, "one", check_by="2026-08-01")
    due_state_module.reconcile(vault, today=TODAY)
    (vault / f"{INSIGHTS}/one.md").unlink()
    find_module.clear_cache()
    assert _served(vault) is None

    monkeypatch.setattr(due_state_module, "_page_exists", lambda *a, **k: True)

    stale = _served(vault)
    assert stale is not None and stale["total"] == 1


def test_missing_state_recomputes_at_serve_time(vault: Path) -> None:
    _prediction(vault, "one", check_by="2026-08-01")
    assert not due_state_module.state_path(vault).exists()

    block = _served(vault)

    assert block is not None and block["total"] == 1
    assert due_state_module.state_path(vault).exists()


@pytest.mark.parametrize(
    "corrupt",
    ["", "{", "null", "[]", '{"version": 999, "categories": {}}', '{"version": 1}'],
)
def test_unreadable_state_recomputes_rather_than_raising(
    vault: Path, corrupt: str
) -> None:
    _prediction(vault, "one", check_by="2026-08-01")
    path = due_state_module.state_path(vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(corrupt, encoding="utf-8")

    block = _served(vault)

    assert block is not None and block["total"] == 1


# ==========================================================================
# egress before counting
# ==========================================================================


def test_a_withheld_item_contributes_zero_everywhere(vault: Path) -> None:
    _prediction(vault, "hidden", check_by="2026-07-01", folder=RESTRICTED)
    _prediction(vault, "visible", check_by="2026-08-01", folder=INSIGHTS)
    _restrict_patterns(vault)
    due_state_module.reconcile(vault, today=TODAY)

    with request_scope(RequestPrincipal(audience_id=EXTERNAL, surface="mcp")):
        _reset_governance_caches()
        block = _served(vault)

    assert block is not None
    assert block["total"] == 1
    assert block["categories"] == {"prediction_window": 1}
    assert [row["due_since"] for row in block["top"]] == ["2026-08-01"]


def test_the_served_view_is_byte_identical_to_a_vault_without_the_item(
    vault: Path, tmp_path: Path
) -> None:
    """Withheld must be indistinguishable from nonexistent — including in ORDERING.

    The withheld prediction is the MOST overdue one, so a projection that
    filtered after ordering, or that counted before filtering, would betray it
    through the shape of what is left.
    """
    _prediction(vault, "hidden", check_by="2026-07-01", folder=RESTRICTED)
    _prediction(vault, "visible", check_by="2026-08-01", folder=INSIGHTS)
    _restrict_patterns(vault)
    due_state_module.reconcile(vault, today=TODAY)
    with request_scope(RequestPrincipal(audience_id=EXTERNAL, surface="mcp")):
        _reset_governance_caches()
        withheld_view = _served(vault)

    (vault / f"{RESTRICTED}/hidden.md").unlink()
    find_module.clear_cache()
    due_state_module.reconcile(vault, today=TODAY)
    with request_scope(RequestPrincipal(audience_id=EXTERNAL, surface="mcp")):
        _reset_governance_caches()
        absent_view = _served(vault)

    assert json.dumps(withheld_view, sort_keys=True) == json.dumps(
        absent_view, sort_keys=True
    )


def test_an_audience_that_may_see_everything_sees_everything(vault: Path) -> None:
    _prediction(vault, "hidden", check_by="2026-07-01", folder=RESTRICTED)
    _prediction(vault, "visible", check_by="2026-08-01", folder=INSIGHTS)
    _restrict_patterns(vault)
    due_state_module.reconcile(vault, today=TODAY)

    with request_scope(RequestPrincipal(audience_id="owner", surface="cli")):
        _reset_governance_caches()
        block = _served(vault)

    assert block is not None
    assert block["total"] == 2


def test_an_ungoverned_vault_pays_no_filter(vault: Path) -> None:
    """No policy → identity filter, and the counts are exactly the projection's."""
    _prediction(vault, "one", check_by="2026-08-01", folder=RESTRICTED)
    due_state_module.reconcile(vault, today=TODAY)

    block = _served(vault)

    assert block is not None and block["total"] == 1


def test_removing_the_egress_filter_fails_this_module(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mechanism-removal for the governance obligation, not just for the counters."""
    _prediction(vault, "hidden", check_by="2026-07-01", folder=RESTRICTED)
    _restrict_patterns(vault)
    due_state_module.reconcile(vault, today=TODAY)

    monkeypatch.setattr(
        egress_module, "release_walk_filter", lambda *a, **k: None
    )
    with request_scope(RequestPrincipal(audience_id=EXTERNAL, surface="mcp")):
        _reset_governance_caches()
        leaked = _served(vault)

    assert leaked is not None and leaked["total"] == 1, (
        "with the filter removed the withheld item must reappear — otherwise "
        "this module never proved the filter was load-bearing"
    )


# ==========================================================================
# review state
# ==========================================================================


def _review_surface_item(vault: Path, rel: str, *, today: dt.date = TODAY):
    """The item exactly as the REVIEW SURFACE composes it: the default union, fused.

    Deliberately not `categories=[one]`. `attention._rank` folds each page's
    unpartitioned page-level signals (relation_debt, stale_review, ...) into the
    partitioned item, so a single-category call is the ONE configuration in which
    no fusion happens -- and a dismissal test written against it proves nothing
    about the surface a user actually triages through.
    """
    from exomem import attention as attention_module

    report = attention_module.attention(vault, limit=0, state="all", today=today)
    matches = [item for item in report.items if item.path == rel]
    assert matches, f"no default-union review item for {rel}"
    return matches[0]


def _assert_fused(item) -> None:
    """The fixture must actually exercise the fusion boundary, or it covers nothing."""
    assert "prediction_window" in item.categories
    assert len(item.categories) > 1, (
        "this page no longer picks up a second, page-level signal, so the item is "
        "no longer FUSED and these tests have stopped crossing the boundary they "
        "exist for -- give the fixture a page-level signal again (a page with no "
        "wikilinks earns relation_debt) rather than relaxing this assertion"
    )


def test_a_dismissal_through_the_review_surface_stops_every_carrier_counting(
    vault: Path,
) -> None:
    """The bug this pins shipped: dismissal never reached the counters.

    `due_state` keys on ONE finding's identity; the review surface hands the user
    a FUSED item whose fingerprint covers every queue that flagged the page. Same
    `item_id`, different fingerprint -- so `effective_state` read the recorded
    dismissal as "the signal materially changed" and kept counting the item on
    every carrier, forever. The fix is one shared composer plus recording the
    decision for each component at decision time.
    """
    from exomem import commands

    rel = _prediction(vault, "one", check_by="2026-08-01")
    due_state_module.reconcile(vault, today=TODAY)
    assert _served(vault)["total"] == 1

    item = _review_surface_item(vault, rel)
    _assert_fused(item)
    entry = due_state_module.served_entries(vault, today=TODAY)[0]
    assert entry["ref"] == item.ref, "same review identity"
    assert entry["fingerprint"] != item.fingerprint, (
        "the two fingerprints genuinely differ -- if they ever coincide this test "
        "passes for the wrong reason"
    )

    commands.op_triage_memory(vault, ref=item.ref, action="dismiss", why="known")

    assert _served(vault) is None


def test_a_snooze_through_the_review_surface_is_quiet_only_until_it_lapses(
    vault: Path,
) -> None:
    from exomem import commands

    rel = _prediction(vault, "one", check_by="2026-08-01")
    due_state_module.reconcile(vault, today=TODAY)
    item = _review_surface_item(vault, rel)
    _assert_fused(item)

    commands.op_triage_memory(
        vault, ref=item.ref, action="snooze", until="2026-08-20", why="after the demo"
    )

    assert _served(vault, today=TODAY) is None
    assert _served(vault, today=dt.date(2026, 8, 20)) is None
    lapsed = _served(vault, today=dt.date(2026, 8, 21))
    assert lapsed is not None and lapsed["total"] == 1


def test_a_reopen_through_the_review_surface_makes_it_count_again(vault: Path) -> None:
    from exomem import commands

    rel = _prediction(vault, "one", check_by="2026-08-01")
    due_state_module.reconcile(vault, today=TODAY)
    item = _review_surface_item(vault, rel)
    _assert_fused(item)

    commands.op_triage_memory(vault, ref=item.ref, action="dismiss", why="known")
    assert _served(vault) is None

    commands.op_triage_memory(vault, ref=item.ref, action="reopen")

    reopened = _served(vault)
    assert reopened is not None and reopened["total"] == 1


def test_a_material_change_resurfaces_a_dismissed_item_under_a_new_fingerprint(
    vault: Path,
) -> None:
    """The other half of the contract: quiet is bound to the signal, not the item."""
    from exomem import commands

    rel = _prediction(vault, "one", check_by="2026-08-01")
    due_state_module.reconcile(vault, today=TODAY)
    item = _review_surface_item(vault, rel)
    before = due_state_module.served_entries(vault, today=TODAY)[0]["fingerprint"]

    commands.op_triage_memory(vault, ref=item.ref, action="dismiss", why="known")
    assert _served(vault) is None

    # The author edits the prediction itself -- new knowledge, not a reformat.
    _write(
        vault,
        rel,
        "---\ntitle: one\ntype: insight\nstatus: active\n"
        "created: 2026-01-01\nupdated: 2026-08-16\n---\n\n"
        "## Prediction\n\n- id: p1\n- check_by: 2026-08-01\n\n"
        "The backlog clears within a DAY, not a week.\n",
    )
    due_state_module.reconcile(vault, today=TODAY)

    after = _served(vault)
    assert after is not None and after["total"] == 1
    changed = due_state_module.served_entries(vault, today=TODAY)[0]["fingerprint"]
    assert changed != before


def test_the_projection_composes_its_identity_through_the_shared_composer(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`due_state` must not compose the per-finding fingerprint privately again.

    Two private composers that must agree is exactly the shape that shipped the
    dismissal bug, so this pins the call rather than the value: if the projection
    stops routing through `review_state.component_fingerprint`, the sentinel never
    appears and this goes red.
    """
    from exomem import review_state as review_state_module

    monkeypatch.setattr(
        review_state_module, "component_fingerprint", lambda **kwargs: "SENTINEL"
    )
    _prediction(vault, "one", check_by="2026-08-01")
    due_state_module.reconcile(vault, today=TODAY)

    rows = due_state_module.served_entries(vault, today=TODAY)
    assert rows and all(row["fingerprint"] == "SENTINEL" for row in rows)


OPT_IN_CASES = [
    (
        "question_aging",
        lambda vault: _question_page(vault, "resolver-budget", created="2026-05-01"),
    ),
    (
        "unfinished_experiments",
        lambda vault: _experiment(vault, "pool-sizing", started="2026-01-01", duration="30 days"),
    ),
]


@pytest.mark.parametrize(("category", "make"), OPT_IN_CASES, ids=[c for c, _ in OPT_IN_CASES])
def test_an_opt_in_categorys_ref_can_be_put_down_through_the_review_surface(
    vault: Path, category: str, make
) -> None:
    """A counter must not hand out a reference nobody can act on.

    `question_aging` and `unfinished_experiments` are registered but opt-in, and
    `item_by_ref` used to resolve only against the default union — so a ref the
    due-state block published was readable on every carrier and resolvable by no
    path at all. `triage_memory` refused it with `REVIEW_ITEM_NOT_FOUND` while
    `due_state_handling` was busy telling the agent to consult its state before
    raising it again.

    The ref used here is the one the BLOCK publishes, not one reconstructed from
    attention, because that is the only ref an agent ever sees.
    """
    from exomem import commands

    make(vault)
    due_state_module.reconcile(vault, today=TODAY)
    rows = [
        row
        for row in due_state_module.served_entries(vault, today=TODAY)
        if row["category"] == category
    ]
    assert len(rows) == 1, f"expected one {category} item, got {len(rows)}"
    ref = rows[0]["ref"]
    assert _served(vault)["categories"][category] == 1

    commands.op_triage_memory(vault, ref=ref, action="dismiss", why="known")
    served = _served(vault)
    assert served is None or category not in served["categories"], (
        f"a dismissed {category} item is still being counted"
    )

    commands.op_triage_memory(vault, ref=ref, action="reopen")
    assert _served(vault)["categories"][category] == 1


@pytest.mark.parametrize(("category", "make"), OPT_IN_CASES, ids=[c for c, _ in OPT_IN_CASES])
def test_removing_the_opt_in_fallback_fails_this_module(
    vault: Path, category: str, make, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mechanism removal, asserted on the OUTCOME because the two break differently.

    Without the wider resolution, `question_aging` raises `REVIEW_ITEM_NOT_FOUND`
    (nothing in the default union sits on its partitioned id) while
    `unfinished_experiments` "succeeds" against the `relation_debt` item that
    shares its bare id — dismissing a different signal and leaving the count up.
    Both are the same product failure: the ref cannot be put down. Asserting the
    exception alone would have let the second, quieter case through, which is how
    it survived the first draft of this test.
    """
    from exomem import attention as attention_module
    from exomem import commands

    monkeypatch.setattr(
        attention_module, "_item_by_ref_fallback", lambda *a, **k: None
    )

    make(vault)
    due_state_module.reconcile(vault, today=TODAY)
    ref = [
        row
        for row in due_state_module.served_entries(vault, today=TODAY)
        if row["category"] == category
    ][0]["ref"]

    try:
        commands.op_triage_memory(vault, ref=ref, action="dismiss", why="known")
    except ValueError as exc:
        assert "REVIEW_ITEM_NOT_FOUND" in str(exc)
        return

    served = _served(vault)
    assert served is not None and served["categories"].get(category) == 1, (
        "triage reported success without the wider resolution, so it must have put "
        "down some OTHER signal while the count stayed up -- if this is green the "
        "mechanism is not load-bearing"
    )


def test_the_fallback_does_not_move_a_default_union_items_identity(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ordering guarantee, pinned: existing refs keep the fingerprint they had.

    The default union is searched first, so an item it holds is returned before
    the fallback runs. This measures that directly — the fingerprint resolved with
    the fallback available must equal the one resolved with it disabled, which is
    exactly the value #555's callers already round-trip into
    `expected_fingerprint`.
    """
    from exomem import attention as attention_module

    rel = _prediction(vault, "one", check_by="2026-08-01")
    item = _review_surface_item(vault, rel)
    _assert_fused(item)

    with_fallback = attention_module.item_by_ref(vault, item.ref, today=TODAY)

    monkeypatch.setattr(
        attention_module, "_item_by_ref_fallback", lambda *a, **k: None
    )
    without_fallback = attention_module.item_by_ref(vault, item.ref, today=TODAY)

    assert with_fallback.item_id == without_fallback.item_id
    assert with_fallback.fingerprint == without_fallback.fingerprint
    assert with_fallback.categories == without_fallback.categories


def test_the_fallback_never_widens_to_every_audit_category(vault: Path) -> None:
    """Bounded on purpose: opt-in REVIEW queues, never the structural checks.

    `ALL_CATEGORIES` carries expensive checks that were never review items;
    resolving a ref against them would turn one triage call into a full audit and
    make things triageable that were never surfaced as items.
    """
    from exomem import attention as attention_module
    from exomem import audit as audit_module

    triageable = set(attention_module._TRIAGEABLE_CATEGORIES)
    assert triageable == set(attention_module.DEFAULT_ATTENTION_CATEGORIES) | set(
        audit_module.EPISTEMIC_REVIEW_CATEGORIES
    )
    assert triageable < set(audit_module.ALL_CATEGORIES)
    assert set(due_state_module.PROJECTION_CATEGORIES) <= triageable, (
        "every category the block can publish a ref for must be resolvable"
    )


def test_removing_the_component_fanout_fails_this_module(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mechanism removal for the fix above.

    If `apply_for_item` stops recording the component identities, the dismissal
    the user made through the review surface stops reaching the counter -- which
    is precisely the shipped bug. A green suite with this monkeypatch in place
    would mean the cross-boundary tests are tautologies again.
    """
    from exomem import commands
    from exomem import review_state as review_state_module

    monkeypatch.setattr(
        review_state_module, "component_fingerprints", lambda *a, **k: []
    )

    rel = _prediction(vault, "one", check_by="2026-08-01")
    due_state_module.reconcile(vault, today=TODAY)
    item = _review_surface_item(vault, rel)
    commands.op_triage_memory(vault, ref=item.ref, action="dismiss", why="known")

    still_counted = _served(vault)
    assert still_counted is not None and still_counted["total"] == 1


def test_the_maintain_memory_reconcile_path_heals_the_projection(vault: Path) -> None:
    """`reconcile` is the product command for "I edited around the system".

    A page changed in Obsidian or on the filesystem fires no write hook, so the
    projection can hold an item the vault no longer owes. Healing it belongs to
    the same command that already heals index counts and the embedding sidecar —
    and it belongs there rather than on the write path, because a full recompute
    must never run inside a mutation's critical section.
    """
    from exomem import commands

    _prediction(vault, "one", check_by="2026-08-01")
    due_state_module.reconcile(vault, today=TODAY)
    assert _served(vault)["total"] == 1

    _prediction(vault, "one", check_by="2026-12-01")
    assert _served(vault)["total"] == 1  # still stale

    commands.op_reconcile(vault)

    assert _served(vault) is None


def test_a_dry_run_reconcile_changes_nothing(vault: Path) -> None:
    from exomem import commands

    _prediction(vault, "one", check_by="2026-08-01")
    due_state_module.reconcile(vault, today=TODAY)
    before = due_state_module.state_path(vault).read_bytes()

    commands.op_reconcile(vault, dry_run=True)

    assert due_state_module.state_path(vault).read_bytes() == before


def test_removing_the_day_boundary_rebucket_fails_this_module(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mechanism-removal for the time-bucket path.

    With promotion frozen to the day the projection was computed, tomorrow's
    prediction stays invisible — which is the pre-change behaviour the whole
    maintained projection exists to replace, and proves the midnight test above
    was measuring the mechanism rather than an accident of ordering.
    """
    _prediction(vault, "tomorrow", check_by=TOMORROW.isoformat())
    due_state_module.reconcile(vault, today=TODAY)

    real_served_entries = due_state_module.served_entries
    monkeypatch.setattr(
        due_state_module,
        "served_entries",
        lambda vault_root, **kwargs: real_served_entries(
            vault_root, **{**kwargs, "today": TODAY}
        ),
    )

    assert due_state_module.served(vault, today=TOMORROW) is None
