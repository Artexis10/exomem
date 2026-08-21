"""The two due-state consumers this change owns: `question_aging`, `supersession_integrity`.

The other two due-state categories — `prediction_window` and `unfinished_experiments` —
shipped in PR #555 under `add-prediction-window-review` and `close-experiment-lifecycle`.
They are CONSUMED by the projection, never restated here, and their behaviour is pinned by
`tests/test_epistemic_review_queues.py`. This module tests only what this change adds.

The split between the two follows the same backlog-profile rule #555 established:

- `question_aging` invents its own threshold (a page has to be N days old before an
  unanswered question is worth raising), so it is registered and selectable but stays OUT
  of the default attention union — the same reasoning that keeps `unfinished_experiments`
  opt-in. It reports a review CANDIDATE at `info`, never a defect.
- `supersession_integrity` invents nothing: a pointer that names a page which does not
  exist, and a chain with two live heads, are defects in state a human authored. It is a
  `warn` and it joins the default union.

`question_aging`'s answering test is deliberately the SAME unit-local shape
`_check_prediction_window` uses — only a `verdict` or a resolving relation authored on the
unit itself clears it — so one resolution rule governs every unit-scoped due-state
category rather than two that drift.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from exomem import attention as attention_module
from exomem import audit as audit_module
from exomem import review_state as review_state_module

TODAY = dt.date(2026, 8, 16)

INSIGHTS = "Knowledge Base/Notes/Insights"
RESEARCH = "Knowledge Base/Notes/Research"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _write(vault: Path, rel: str, text: str) -> str:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return rel


def _question_page(
    vault: Path,
    slug: str,
    *,
    created: str,
    status: str = "active",
    verdict: str | None = None,
    relation: str | None = None,
    content: str = "Does the projection survive a day boundary with no write?",
    anchor: str = "q1",
) -> str:
    head = (
        "---\n"
        f"title: {slug}\n"
        "type: insight\n"
        f"status: {status}\n"
        f"created: {created}\n"
        f"updated: {created}\n"
        "---\n\n"
    )
    rows = [f"- id: {anchor}"]
    if verdict is not None:
        rows.append(f"- verdict: {verdict}")
    if relation is not None:
        rows.append(f"- relations: {relation}")
    block = "## Open Question\n\n" + "\n".join(rows) + f"\n\n{content}\n"
    return _write(vault, f"{INSIGHTS}/{slug}.md", head + block)


def _superseded_page(
    vault: Path,
    slug: str,
    *,
    status: str = "superseded",
    superseded_by: str | None = None,
    supersedes: str | None = None,
    folder: str = RESEARCH,
    updated: str = "2026-06-01",
) -> str:
    lines = [
        "---",
        f"title: {slug}",
        "type: research-note",
        f"status: {status}",
        "created: 2026-01-01",
        f"updated: {updated}",
    ]
    if superseded_by is not None:
        lines.append(f'superseded_by: "[[{superseded_by}]]"')
    if supersedes is not None:
        lines.append(f'supersedes: "[[{supersedes}]]"')
    lines += ["---", "", "# " + slug, "", "Body.", ""]
    return _write(vault, f"{folder}/{slug}.md", "\n".join(lines))


def _findings(vault: Path, category: str) -> list[audit_module.AuditFinding]:
    report = audit_module.audit(vault, categories=[category], today=TODAY)
    return [f for f in report.findings if f.category == category]


def _paths(findings: list[audit_module.AuditFinding]) -> list[str]:
    return [f.path for f in findings]


def _item(vault: Path, category: str, path: str) -> attention_module.AttentionItem:
    report = attention_module.attention(
        vault, categories=[category], limit=0, state="all", today=TODAY
    )
    matches = [item for item in report.items if item.path == path]
    assert matches, f"no {category} item for {path}"
    return matches[0]


# ==========================================================================
# registration
# ==========================================================================


def test_both_categories_are_registered_audit_categories() -> None:
    assert "question_aging" in audit_module.ALL_CATEGORIES
    assert "supersession_integrity" in audit_module.ALL_CATEGORIES


def test_question_aging_is_registered_but_not_default_in_attention() -> None:
    """Its threshold is system-invented, so it must not displace the daily surface.

    Same rule `unfinished_experiments` is held to (audit.EPISTEMIC_REVIEW_CATEGORIES).
    """
    assert "question_aging" in attention_module.ATTENTION_CATEGORIES
    assert "question_aging" not in attention_module.DEFAULT_ATTENTION_CATEGORIES


def test_supersession_integrity_joins_the_default_attention_union() -> None:
    """The pointer is authored and nothing is inferred, so there is no threshold to hide."""
    assert "supersession_integrity" in attention_module.DEFAULT_ATTENTION_CATEGORIES


# ==========================================================================
# question_aging
# ==========================================================================


def test_an_aging_unanswered_question_surfaces_as_a_candidate(vault: Path) -> None:
    rel = _question_page(vault, "resolver-budget", created="2026-05-01")

    findings = _findings(vault, "question_aging")

    assert _paths(findings) == [rel]
    finding = findings[0]
    assert finding.severity == "info"
    assert finding.meta is not None
    assert finding.meta["age_days"] == 107
    assert finding.meta["authored"] == "2026-05-01"
    assert finding.meta["signal_version"]
    assert finding.meta["review_partition"] == finding.meta["signal_version"]
    # A candidate, never a defect: the wording has to say so, because the whole
    # justification for admitting a system-invented threshold is that it never
    # claims anything is wrong.
    assert "candidate" in finding.detail.lower()
    assert "never a defect" in finding.detail.lower()


def test_a_question_younger_than_the_threshold_is_not_surfaced(vault: Path) -> None:
    _question_page(vault, "fresh", created="2026-08-01")

    assert _findings(vault, "question_aging") == []


def test_the_threshold_edge_is_inclusive(vault: Path) -> None:
    """`>= QUESTION_AGING_DAYS`, so the boundary day itself surfaces."""
    exact = (TODAY - dt.timedelta(days=audit_module.QUESTION_AGING_DAYS)).isoformat()
    rel = _question_page(vault, "edge", created=exact)

    assert _paths(_findings(vault, "question_aging")) == [rel]

    inside = (
        TODAY - dt.timedelta(days=audit_module.QUESTION_AGING_DAYS - 1)
    ).isoformat()
    _question_page(vault, "edge", created=inside)
    assert _findings(vault, "question_aging") == []


def test_a_page_with_no_parseable_authored_date_is_never_surfaced(vault: Path) -> None:
    """Absent optional fields mean what they mean today — never invent an age."""
    _write(
        vault,
        f"{INSIGHTS}/undated.md",
        "---\ntitle: undated\ntype: insight\nstatus: active\n---\n\n"
        "## Open Question\n\n- id: q1\n\nStill open?\n",
    )

    assert _findings(vault, "question_aging") == []


def test_a_verdict_on_the_unit_answers_the_question(vault: Path) -> None:
    _question_page(vault, "verdicted", created="2026-05-01", verdict="confirmed")

    assert _findings(vault, "question_aging") == []


@pytest.mark.parametrize(
    "kind", ["resolves", "supports", "contradicts", "evidenced_by"]
)
def test_an_answering_relation_on_the_unit_clears_it(vault: Path, kind: str) -> None:
    _question_page(
        vault,
        "answered",
        created="2026-05-01",
        relation=f"{kind}: [[{INSIGHTS}/autovacuum]]",
    )

    assert _findings(vault, "question_aging") == []


def test_a_non_answering_relation_does_not_clear_it(vault: Path) -> None:
    rel = _question_page(
        vault,
        "related-only",
        created="2026-05-01",
        relation=f"relates_to: [[{INSIGHTS}/autovacuum]]",
    )

    assert _paths(_findings(vault, "question_aging")) == [rel]


def test_a_relation_on_a_sibling_unit_does_not_clear_it(vault: Path) -> None:
    """Unit-local, exactly like `prediction_window`: a neighbour cannot answer for you."""
    head = (
        "---\ntitle: siblings\ntype: insight\nstatus: active\n"
        "created: 2026-05-01\nupdated: 2026-05-01\n---\n\n"
    )
    rel = _write(
        vault,
        f"{INSIGHTS}/siblings.md",
        head
        + "## Open Question\n\n- id: q1\n\nStill open?\n\n"
        + f"## Open Question\n\n- id: q2\n- relations: resolves: [[{INSIGHTS}/x]]\n\nAnswered.\n",
    )

    findings = _findings(vault, "question_aging")
    assert _paths(findings) == [rel]
    assert findings[0].meta["anchor"] == "q1"


@pytest.mark.parametrize("status", ["superseded", "archived", "draft", "dropped"])
def test_a_parked_page_is_out_of_rotation(vault: Path, status: str) -> None:
    _question_page(vault, "parked", created="2026-05-01", status=status)

    assert _findings(vault, "question_aging") == []


def test_question_aging_is_ordered_oldest_first(vault: Path) -> None:
    older = _question_page(vault, "older", created="2026-01-01")
    newer = _question_page(vault, "newer", created="2026-06-01")

    assert _paths(_findings(vault, "question_aging")) == [older, newer]


def test_question_aging_writes_nothing(vault: Path) -> None:
    _question_page(vault, "resolver-budget", created="2026-05-01")
    before = sorted(p.relative_to(vault).as_posix() for p in vault.rglob("*") if p.is_file())

    _findings(vault, "question_aging")

    after = sorted(p.relative_to(vault).as_posix() for p in vault.rglob("*") if p.is_file())
    assert before == after


def test_removing_the_question_aging_mechanism_fails_this_module(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mechanism-removal: monkeypatch the predicate away and the queue must go silent."""
    _question_page(vault, "resolver-budget", created="2026-05-01")
    assert _findings(vault, "question_aging")

    monkeypatch.setattr(
        audit_module, "_check_question_aging", lambda *a, **k: []
    )
    assert _findings(vault, "question_aging") == []


# ==========================================================================
# supersession_integrity
# ==========================================================================


def test_a_dangling_superseded_by_pointer_is_a_defect(vault: Path) -> None:
    rel = _superseded_page(
        vault, "old-view", superseded_by=f"{RESEARCH}/a-page-that-does-not-exist"
    )

    findings = _findings(vault, "supersession_integrity")

    assert _paths(findings) == [rel]
    finding = findings[0]
    assert finding.severity == "warn"
    assert finding.meta["defect"] == "dangling_pointer"
    assert finding.meta["pointer"] == "superseded_by"
    assert finding.meta["signal_version"]
    assert finding.meta["review_partition"]


def test_a_dangling_supersedes_pointer_is_a_defect(vault: Path) -> None:
    rel = _superseded_page(
        vault,
        "new-view",
        status="active",
        supersedes=f"{RESEARCH}/also-missing",
    )

    findings = _findings(vault, "supersession_integrity")

    assert _paths(findings) == [rel]
    assert findings[0].meta["pointer"] == "supersedes"


def test_a_resolving_pointer_pair_is_clean(vault: Path) -> None:
    _superseded_page(vault, "old-view", superseded_by=f"{RESEARCH}/new-view")
    _superseded_page(
        vault, "new-view", status="active", supersedes=f"{RESEARCH}/old-view"
    )

    assert _findings(vault, "supersession_integrity") == []


def test_a_page_with_no_supersession_pointer_is_never_surfaced(vault: Path) -> None:
    _superseded_page(vault, "plain", status="active")

    assert _findings(vault, "supersession_integrity") == []


def test_a_forked_chain_reports_more_than_one_current_head(vault: Path) -> None:
    """Two successors, neither superseded — the chain no longer has one current answer."""
    _superseded_page(vault, "origin", superseded_by=f"{RESEARCH}/head-a")
    _superseded_page(
        vault, "head-a", status="active", supersedes=f"{RESEARCH}/origin"
    )
    _superseded_page(
        vault, "head-b", status="active", supersedes=f"{RESEARCH}/origin"
    )

    findings = _findings(vault, "supersession_integrity")

    forks = [f for f in findings if f.meta["defect"] == "multi_headed_chain"]
    assert forks, f"expected a multi-head defect, got {[f.meta for f in findings]}"
    assert all(f.severity == "warn" for f in forks)
    heads = forks[0].meta["heads"]
    assert sorted(heads) == sorted(
        [f"{RESEARCH}/head-a.md", f"{RESEARCH}/head-b.md"]
    )


def test_a_linear_chain_has_exactly_one_head(vault: Path) -> None:
    _superseded_page(vault, "v1", superseded_by=f"{RESEARCH}/v2")
    _superseded_page(
        vault,
        "v2",
        superseded_by=f"{RESEARCH}/v3",
        supersedes=f"{RESEARCH}/v1",
    )
    _superseded_page(
        vault, "v3", status="active", supersedes=f"{RESEARCH}/v2"
    )

    assert _findings(vault, "supersession_integrity") == []


def test_supersession_integrity_writes_nothing(vault: Path) -> None:
    _superseded_page(vault, "old-view", superseded_by=f"{RESEARCH}/gone")
    before = sorted(p.relative_to(vault).as_posix() for p in vault.rglob("*") if p.is_file())

    _findings(vault, "supersession_integrity")

    after = sorted(p.relative_to(vault).as_posix() for p in vault.rglob("*") if p.is_file())
    assert before == after


def test_removing_the_supersession_mechanism_fails_this_module(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _superseded_page(vault, "old-view", superseded_by=f"{RESEARCH}/gone")
    assert _findings(vault, "supersession_integrity")

    monkeypatch.setattr(
        audit_module, "_check_supersession_integrity", lambda *a, **k: []
    )
    assert _findings(vault, "supersession_integrity") == []


# ==========================================================================
# shared review-item semantics (fingerprint, dismissal, material change)
# ==========================================================================


def test_question_aging_composes_a_review_item_with_a_stable_fingerprint(
    vault: Path,
) -> None:
    rel = _question_page(vault, "resolver-budget", created="2026-05-01")

    first = _item(vault, "question_aging", rel)
    second = _item(vault, "question_aging", rel)

    assert first.fingerprint == second.fingerprint
    assert first.item_id == second.item_id
    assert first.ref and first.ref.startswith(review_state_module.REVIEW_PREFIX)
    assert first.state == "open"


def test_supersession_integrity_composes_a_review_item_with_a_stable_fingerprint(
    vault: Path,
) -> None:
    rel = _superseded_page(vault, "old-view", superseded_by=f"{RESEARCH}/gone")

    first = _item(vault, "supersession_integrity", rel)
    second = _item(vault, "supersession_integrity", rel)

    assert first.fingerprint == second.fingerprint
    assert first.state == "open"


@pytest.mark.parametrize(
    ("category", "make", "mutate"),
    [
        (
            "question_aging",
            lambda vault: _question_page(vault, "q", created="2026-05-01"),
            lambda vault: _question_page(
                vault,
                "q",
                created="2026-05-01",
                content="Does the projection survive TWO day boundaries?",
            ),
        ),
        (
            "supersession_integrity",
            lambda vault: _superseded_page(
                vault, "s", superseded_by=f"{RESEARCH}/gone"
            ),
            lambda vault: _superseded_page(
                vault,
                "s",
                superseded_by=f"{RESEARCH}/gone",
                updated="2026-07-15",
            ),
        ),
    ],
)
def test_dismissal_holds_until_the_page_changes_materially(
    vault: Path, category: str, make, mutate
) -> None:
    """Exactly the queue-wide contract: a dismissed fingerprint never comes back,
    and a materially changed page surfaces as a NEW fingerprint."""
    rel = make(vault)
    item = _item(vault, category, rel)
    store = review_state_module.ReviewStateStore(vault)
    store.apply(item.item_id, item.fingerprint, action="dismiss", why="not now")

    after = _item(vault, category, rel)
    assert after.state == "dismissed"

    mutate(vault)
    changed = _item(vault, category, rel)
    assert changed.fingerprint != item.fingerprint
    assert changed.state == "open"
