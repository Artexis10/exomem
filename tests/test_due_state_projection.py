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
    """A file changed by Obsidian, git, or a human — no write hook fired."""
    _prediction(vault, "one", check_by="2026-08-01")
    due_state_module.reconcile(vault, today=TODAY)
    assert _served(vault)["total"] == 1

    (vault / f"{INSIGHTS}/one.md").unlink()
    find_module.clear_cache()

    # The stale projection still claims the item until the healer runs.
    assert _served(vault)["total"] == 1

    due_state_module.reconcile(vault, today=TODAY)
    assert _served(vault) is None


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


def test_a_dismissed_item_stops_counting(vault: Path) -> None:
    from exomem import attention as attention_module
    from exomem import review_state as review_state_module

    _prediction(vault, "one", check_by="2026-08-01")
    due_state_module.reconcile(vault, today=TODAY)
    assert _served(vault)["total"] == 1

    report = attention_module.attention(
        vault, categories=["prediction_window"], limit=0, state="all", today=TODAY
    )
    item = report.items[0]
    store = review_state_module.ReviewStateStore(vault)
    entry = due_state_module.served_entries(vault, today=TODAY)[0]
    store.apply(item.item_id, entry["fingerprint"], action="dismiss", why="known")

    assert _served(vault) is None


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

    (vault / f"{INSIGHTS}/one.md").unlink()
    find_module.clear_cache()
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
