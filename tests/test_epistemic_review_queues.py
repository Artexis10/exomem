"""Epistemic review queues: unfinished experiments and closed prediction windows.

Two lifecycle checks that close loops the product already opened.

`unfinished_experiments` closes a contract the shipped scaffold already promises
users — an experiment whose declared window elapsed without a recorded `outcome:`.
The trigger is the missing outcome, not `status: active`: a `concluded` experiment
with no outcome recorded is the purest instance of the thing, because the status
says the experiment stopped and the outcome says what it showed.

`prediction_window` closes the epistemic loop's last step — an authored `check_by`
that has come due with nothing recorded against it. Its resolution test is
deliberately unit-local (see the change's `design.md`): only a `verdict` or an
outbound resolving relation *on the unit itself* clears it, because
`epistemic_graph` still strips `#fragment` off a relation target, so an inbound
edge cannot address a unit today.

The two are split on backlog profile, not category kind. `prediction_window`
joins the DEFAULT attention union, because `check_by` shipped a day before the
queue and no vault can hold a grandfathered population of due predictions.
`unfinished_experiments` stays opt-in, because `started`/`duration` predate the
package rename and an established vault can hold dozens of long-closed windows.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from pathlib import Path

import pytest

from exomem import attention as attention_module
from exomem import audit as audit_module

TODAY = dt.date(2026, 8, 16)

EXPERIMENTS = "Knowledge Base/Notes/Experiments/Infrastructure"
INSIGHTS = "Knowledge Base/Notes/Insights"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _write(vault: Path, rel: str, text: str) -> str:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return rel


def _experiment(
    vault: Path,
    slug: str,
    *,
    started: str,
    duration: str,
    status: str = "active",
    outcome: str | None = None,
) -> str:
    lines = [
        "---",
        f"title: {slug}",
        "type: experiment",
        "domain: infrastructure",
        f"status: {status}",
        "created: 2025-01-01",
        "updated: 2025-01-01",
        f"started: {started}",
        f'duration: "{duration}"',
        "n: 1",
    ]
    if outcome is not None:
        lines.append(f"outcome: {outcome}")
    lines += ["---", "", "## Hypothesis", "", "It will work.", ""]
    return _write(vault, f"{EXPERIMENTS}/{slug}.md", "\n".join(lines))


def _prediction_page(vault: Path, slug: str, body: str, *, status: str = "active") -> str:
    head = (
        "---\n"
        f"title: {slug}\n"
        "type: insight\n"
        f"status: {status}\n"
        "created: 2026-01-01\n"
        "updated: 2026-01-01\n"
        "---\n\n"
    )
    return _write(vault, f"{INSIGHTS}/{slug}.md", head + body.rstrip() + "\n")


def _prediction_block(
    anchor: str,
    *,
    check_by: str | None,
    verdict: str | None = None,
    relation: str | None = None,
    content: str = "The autovacuum backlog clears within a week.",
) -> str:
    rows = [f"- id: {anchor}"]
    if check_by is not None:
        rows.append(f"- check_by: {check_by}")
    if verdict is not None:
        rows.append(f"- verdict: {verdict}")
    if relation is not None:
        rows.append(f"- relations: {relation}")
    return "## Prediction\n\n" + "\n".join(rows) + f"\n\n{content}\n"


def _findings(vault: Path, category: str) -> list[audit_module.AuditFinding]:
    report = audit_module.audit(vault, categories=[category], today=TODAY)
    return [f for f in report.findings if f.category == category]


def _paths(findings: list[audit_module.AuditFinding]) -> list[str]:
    return [f.path for f in findings]


def _vault_snapshot(vault: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for path in sorted(vault.rglob("*")):
        if path.is_file():
            rel = path.relative_to(vault).as_posix()
            out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


# ==========================================================================
# Change A — unfinished_experiments
# ==========================================================================


def test_unfinished_experiments_is_a_registered_audit_category() -> None:
    assert "unfinished_experiments" in audit_module.ALL_CATEGORIES


def test_experiment_past_its_window_without_outcome_is_surfaced(vault: Path) -> None:
    rel = _experiment(vault, "vacuum-tuning", started="2026-04-18", duration="30 days")

    findings = _findings(vault, "unfinished_experiments")

    assert _paths(findings) == [rel]
    finding = findings[0]
    assert finding.severity == "info"
    assert finding.meta is not None
    assert finding.meta["elapsed_days"] == 120
    assert finding.meta["duration_days"] == 30
    assert finding.meta["overdue_days"] == 90
    assert finding.meta["started"] == "2026-04-18"
    assert finding.meta["signal_version"]


def test_recorded_outcome_closes_the_experiment_loop(vault: Path) -> None:
    _experiment(
        vault,
        "vacuum-tuning",
        started="2026-04-18",
        duration="30 days",
        status="concluded",
        outcome="confirmed",
    )

    assert _findings(vault, "unfinished_experiments") == []


def test_concluded_without_outcome_still_surfaces(vault: Path) -> None:
    """`status` says it stopped; `outcome` says what it showed. Only the second closes."""
    rel = _experiment(
        vault,
        "vacuum-tuning",
        started="2026-04-18",
        duration="30 days",
        status="concluded",
    )

    assert _paths(_findings(vault, "unfinished_experiments")) == [rel]


@pytest.mark.parametrize("duration", ["ongoing", "until it stops helping", ""])
def test_open_ended_duration_is_never_overdue(vault: Path, duration: str) -> None:
    _experiment(vault, "long-haul", started="2020-01-01", duration=duration)

    assert _findings(vault, "unfinished_experiments") == []


def test_experiment_inside_its_window_is_not_surfaced(vault: Path) -> None:
    _experiment(vault, "fresh", started="2026-08-06", duration="30 days")

    assert _findings(vault, "unfinished_experiments") == []


def test_experiment_exactly_at_its_window_edge_is_not_surfaced(vault: Path) -> None:
    """Elapsed == duration is still inside the window; only exceeding it counts."""
    _experiment(vault, "edge", started="2026-07-17", duration="30 days")

    assert _findings(vault, "unfinished_experiments") == []


@pytest.mark.parametrize("status", ["archived", "superseded", "draft"])
def test_out_of_rotation_experiments_are_excluded(vault: Path, status: str) -> None:
    _experiment(
        vault, "parked", started="2020-01-01", duration="30 days", status=status
    )

    assert _findings(vault, "unfinished_experiments") == []


def test_unfinished_experiment_queue_is_ordered_oldest_first(vault: Path) -> None:
    newest = _experiment(vault, "b-newest", started="2026-06-01", duration="7 days")
    oldest = _experiment(vault, "a-oldest", started="2024-01-01", duration="7 days")
    middle = _experiment(vault, "c-middle", started="2025-06-01", duration="7 days")

    assert _paths(_findings(vault, "unfinished_experiments")) == [
        oldest,
        middle,
        newest,
    ]


def test_unfinished_experiment_duration_units_are_understood(vault: Path) -> None:
    """A week is 7 days, so a 60-day-old 2-week experiment is overdue by 46."""
    rel = _experiment(vault, "weeks", started="2026-06-17", duration="2 weeks")

    findings = _findings(vault, "unfinished_experiments")
    assert _paths(findings) == [rel]
    assert findings[0].meta is not None
    assert findings[0].meta["duration_days"] == 14
    assert findings[0].meta["overdue_days"] == 46


def test_unfinished_experiments_check_writes_nothing(vault: Path) -> None:
    _experiment(vault, "vacuum-tuning", started="2026-04-18", duration="30 days")
    before = _vault_snapshot(vault)

    _findings(vault, "unfinished_experiments")

    assert _vault_snapshot(vault) == before


def test_unfinished_experiments_is_selectable_but_not_default_in_attention(
    vault: Path,
) -> None:
    rel = _experiment(vault, "vacuum-tuning", started="2026-04-18", duration="30 days")

    assert "unfinished_experiments" in attention_module.ATTENTION_CATEGORIES
    assert "unfinished_experiments" not in attention_module.DEFAULT_ATTENTION_CATEGORIES

    selected = attention_module.attention(
        vault, categories=["unfinished_experiments"], today=TODAY
    )
    assert [item.path for item in selected.items] == [rel]
    assert selected.items[0].categories == ["unfinished_experiments"]

    default = attention_module.attention(vault, today=TODAY)
    assert all(
        "unfinished_experiments" not in item.categories for item in default.items
    )


def test_default_attention_union_is_pinned() -> None:
    """The default union is a deliberate list, so widening it must be deliberate.

    This assertion exists to FAIL when someone adds a category to the daily
    review surface without arguing for it. Adding one is legitimate — this
    branch does it for `prediction_window` — but it costs an explicit edit here
    and, one level up, a MODIFIED delta against the attention-queue spec that
    pins the same order normatively.

    The ordering runs from explicitly AUTHORED commitments to INFERRED signals:
    a governance review date and an epistemic check date are both dates a human
    wrote down, so they outrank a cosine proximity band, an age heuristic, an
    empty-field scan, and a missing-edge scan.

    Widened once since, deliberately, for `supersession_integrity`
    (`add-due-state-consumers-and-carriers`). Its argument is of the same kind
    rather than of seniority: it is the only DEFECT queue in the union. The two
    above it report an authored obligation that has come DUE, which is work; a
    supersession pointer that resolves to nothing, or a chain with two live
    heads, reports state that is already WRONG, and nothing about it is inferred
    or thresholded. It sits below the dated queues because a broken pointer does
    not expire while a check date does, and above the inferential ones because it
    invents nothing.

    That change states its position normatively in the ADDED requirement of its
    own `attention-queue` delta rather than in a third concurrent MODIFIED delta
    against this same requirement. Two unarchived changes already carry one
    (`add-prediction-window-review` and this branch's own history), and a third
    would collide at archive-sync — which is the exact failure that change's
    task 0 exists to prevent. The normative record is present either way.
    """
    assert attention_module.DEFAULT_ATTENTION_CATEGORIES == (
        "bridge_review",
        "prediction_window",
        "supersession_integrity",
        "corpus_contradictions",
        "stale_review",
        "unprocessed_source",
        "relation_debt",
    )


def test_scaffold_documents_the_implemented_experiment_predicate() -> None:
    """The shipped doc must describe the check that runs, not one that does not."""
    doc = (
        Path(audit_module.__file__).parent
        / "_scaffold"
        / "_Schema"
        / "references"
        / "audit-checks.md"
    ).read_text(encoding="utf-8")

    entry = next(
        line for line in doc.splitlines() if "Unfinished experiments" in line
    )
    assert "unfinished_experiments" in entry
    assert "outcome" in entry
    assert "ongoing" in entry
    assert "status: active" not in entry


# ==========================================================================
# Change B — prediction_window
# ==========================================================================


def test_prediction_window_is_a_registered_audit_category() -> None:
    assert "prediction_window" in audit_module.ALL_CATEGORIES


def test_due_prediction_without_verdict_or_relation_is_surfaced(vault: Path) -> None:
    rel = _prediction_page(
        vault, "autovacuum", _prediction_block("p1", check_by="2026-08-02")
    )

    findings = _findings(vault, "prediction_window")

    assert _paths(findings) == [rel]
    finding = findings[0]
    assert finding.severity == "info"
    assert finding.meta is not None
    assert finding.meta["overdue_days"] == 14
    assert finding.meta["check_by"] == "2026-08-02"
    assert finding.meta["anchor"] == "p1"
    assert finding.meta["kind"] == "prediction"


def test_verdict_clears_the_prediction_window(vault: Path) -> None:
    _prediction_page(
        vault,
        "autovacuum",
        _prediction_block("p1", check_by="2026-08-02", verdict="refuted"),
    )

    assert _findings(vault, "prediction_window") == []


@pytest.mark.parametrize(
    "kind", ["supports", "contradicts", "resolves", "evidenced_by"]
)
def test_outbound_resolving_relation_clears_the_window(vault: Path, kind: str) -> None:
    _prediction_page(
        vault,
        "autovacuum",
        _prediction_block(
            "p1",
            check_by="2026-08-02",
            relation=f"{kind}: [[Knowledge Base/Notes/Insights/autovacuum]]",
        ),
    )

    assert _findings(vault, "prediction_window") == []


def test_non_resolving_relation_does_not_clear_the_window(vault: Path) -> None:
    rel = _prediction_page(
        vault,
        "autovacuum",
        _prediction_block(
            "p1",
            check_by="2026-08-02",
            relation="relates_to: [[Knowledge Base/Notes/Insights/autovacuum]]",
        ),
    )

    assert _paths(_findings(vault, "prediction_window")) == [rel]


def test_resolution_is_unit_local_not_page_local(vault: Path) -> None:
    """A sibling unit's resolving relation must not clear an untouched prediction."""
    rel = _prediction_page(
        vault,
        "autovacuum",
        _prediction_block("due", check_by="2026-08-02")
        + "\n"
        + _prediction_block(
            "settled",
            check_by="2026-08-02",
            relation="resolves: [[Knowledge Base/Notes/Insights/autovacuum]]",
            content="A different prediction, already engaged with.",
        ),
    )

    findings = _findings(vault, "prediction_window")
    assert _paths(findings) == [rel]
    assert findings[0].meta is not None
    assert findings[0].meta["anchor"] == "due"


def test_check_by_today_is_due_and_tomorrow_is_not(vault: Path) -> None:
    due = _prediction_page(
        vault, "today", _prediction_block("p1", check_by=TODAY.isoformat())
    )
    _prediction_page(
        vault,
        "tomorrow",
        _prediction_block("p2", check_by=(TODAY + dt.timedelta(days=1)).isoformat()),
    )

    findings = _findings(vault, "prediction_window")
    assert _paths(findings) == [due]
    assert findings[0].meta is not None
    assert findings[0].meta["overdue_days"] == 0


def test_unit_without_check_by_is_never_surfaced(vault: Path) -> None:
    _prediction_page(vault, "undated", _prediction_block("p1", check_by=None))

    assert _findings(vault, "prediction_window") == []


@pytest.mark.parametrize("status", ["superseded", "archived", "draft"])
def test_out_of_rotation_pages_are_excluded_from_prediction_window(
    vault: Path, status: str
) -> None:
    _prediction_page(
        vault,
        "parked",
        _prediction_block("p1", check_by="2020-01-01"),
        status=status,
    )

    assert _findings(vault, "prediction_window") == []


def test_prediction_window_queue_is_ordered_most_overdue_first(vault: Path) -> None:
    recent = _prediction_page(
        vault, "b-recent", _prediction_block("p1", check_by="2026-08-10")
    )
    ancient = _prediction_page(
        vault, "a-ancient", _prediction_block("p2", check_by="2024-01-01")
    )
    middle = _prediction_page(
        vault, "c-middle", _prediction_block("p3", check_by="2025-06-01")
    )

    assert _paths(_findings(vault, "prediction_window")) == [ancient, middle, recent]


def test_prediction_window_check_writes_nothing(vault: Path) -> None:
    _prediction_page(
        vault, "autovacuum", _prediction_block("p1", check_by="2026-08-02")
    )
    before = _vault_snapshot(vault)

    _findings(vault, "prediction_window")

    assert _vault_snapshot(vault) == before


def test_prediction_finding_is_identified_by_unit_fingerprint(vault: Path) -> None:
    from exomem import relation_registry, semantic_units

    rel = _prediction_page(
        vault, "autovacuum", _prediction_block("p1", check_by="2026-08-02")
    )
    body = (vault / rel).read_text(encoding="utf-8").split("---\n", 2)[-1]
    document = semantic_units.parse_semantic_units(
        body,
        path=rel,
        validate=False,
        relation_registry=relation_registry.load_registry(vault),
    )
    expected = document.rich_units[0].fingerprint

    finding = _findings(vault, "prediction_window")[0]

    assert finding.meta is not None
    assert finding.meta["signal_version"] == expected
    assert finding.meta["review_partition"] == expected


def test_editing_a_prediction_moves_its_signal_version(vault: Path) -> None:
    rel = _prediction_page(
        vault, "autovacuum", _prediction_block("p1", check_by="2026-08-02")
    )
    before = _findings(vault, "prediction_window")[0].meta["signal_version"]

    _prediction_page(
        vault,
        "autovacuum",
        _prediction_block(
            "p1",
            check_by="2026-08-02",
            content="The autovacuum backlog clears within a fortnight.",
        ),
    )
    after = _findings(vault, "prediction_window")[0].meta["signal_version"]

    assert (vault / rel).exists()
    assert after != before


def test_two_due_predictions_on_one_page_are_two_review_items(vault: Path) -> None:
    rel = _prediction_page(
        vault,
        "autovacuum",
        _prediction_block("first", check_by="2026-08-02")
        + "\n"
        + _prediction_block(
            "second", check_by="2026-07-02", content="A second, distinct prediction."
        ),
    )

    report = attention_module.attention(
        vault, categories=["prediction_window"], today=TODAY
    )

    assert [item.path for item in report.items] == [rel, rel]
    assert len({item.item_id for item in report.items}) == 2
    assert len({item.fingerprint for item in report.items}) == 2


def test_prediction_window_is_in_the_default_attention_union(vault: Path) -> None:
    """A due prediction must reach the daily surface without being asked for.

    Unlike `unfinished_experiments`, this vocabulary is one day old, so no vault
    can hold a grandfathered backlog to flood the queue with. Opt-in would buy
    nothing and cost the whole point: a prediction that surfaces only when you
    already thought to ask about predictions has not closed any loop.
    """
    rel = _prediction_page(
        vault, "autovacuum", _prediction_block("p1", check_by="2026-08-02")
    )

    assert "prediction_window" in attention_module.DEFAULT_ATTENTION_CATEGORIES
    assert "prediction_window" in attention_module.ATTENTION_CATEGORIES

    default = attention_module.attention(vault, today=TODAY)
    assert rel in [item.path for item in default.items]
    surfaced = next(item for item in default.items if item.path == rel)
    assert "prediction_window" in surfaced.categories

    selected = attention_module.attention(
        vault, categories=["prediction_window"], today=TODAY
    )
    assert [item.path for item in selected.items] == [rel]


def test_prediction_window_outranks_the_inferred_queues_on_a_tie() -> None:
    """Tiebreak order: an authored check date beats a cosine band and an age heuristic."""
    findings = [
        audit_module.AuditFinding(
            category=category,
            severity="info",
            path=f"{category}.md",
            detail=category,
            proposed_fix="x",
        )
        for category in (
            "corpus_contradictions",
            "stale_review",
            "prediction_window",
            "relation_debt",
        )
    ]

    report = attention_module._rank(findings)

    assert [item.categories[0] for item in report.items] == [
        "prediction_window",
        "corpus_contradictions",
        "stale_review",
        "relation_debt",
    ]


def test_only_prediction_window_joins_the_default_union(vault: Path) -> None:
    """The two queues are split on backlog profile, and the split must be visible.

    `unfinished_experiments` reads `started`/`duration`, which predate the
    package rename, so a long-lived vault can hold dozens of long-closed
    windows. `prediction_window` reads `check_by`, which shipped with the
    epistemic loop primitives one day before this change. Same mechanism,
    opposite grandfathered population, so opposite default.
    """
    _experiment(vault, "vacuum-tuning", started="2026-04-18", duration="30 days")
    prediction = _prediction_page(
        vault, "autovacuum", _prediction_block("p1", check_by="2026-08-02")
    )

    default = attention_module.attention(vault, today=TODAY)
    surfaced = {
        category for item in default.items for category in item.categories
    }

    assert "prediction_window" in surfaced
    assert "unfinished_experiments" not in surfaced
    assert prediction in [item.path for item in default.items]


# ==========================================================================
# Review fixes — prefilter fidelity, row collision, scope guards, scaffold
# ==========================================================================


@pytest.mark.parametrize(
    "row", ["check_by", "Check By", "check by", "check-by", "CHECK_BY"]
)
def test_every_spelling_the_parser_accepts_is_surfaced(vault: Path, row: str) -> None:
    """The cheap prefilter must not be narrower than the parser it guards.

    `semantic_blocks.normalize_label` lowercases and maps `[\\s-]+` to `_`, so
    all five of these author a genuine governed `check_by` and `find` already
    surfaces every one of them. A raw case-sensitive substring prefilter drops
    four, and for a queue whose entire justification is that an unsurfaced
    obligation is one nobody meets, a silent miss is the worst failure it has.
    """
    rel = _prediction_page(
        vault,
        "autovacuum",
        f"## Prediction\n\n- id: p1\n- {row}: 2026-08-02\n\nBacklog clears.\n",
    )

    findings = _findings(vault, "prediction_window")

    assert _paths(findings) == [rel], f"{row!r} parsed a check_by but was not surfaced"
    assert findings[0].meta is not None
    assert findings[0].meta["check_by"] == "2026-08-02"


def test_partitioned_and_unpartitioned_signals_compose_one_row() -> None:
    """A note flagged by a partitioned queue AND a page-level queue is ONE item.

    `_rank` anchors a partitioned finding on `path\\0partition`, which shares no
    key with the same page's unpartitioned findings. Left alone, the note takes
    two rows of the daily surface and its RRF votes never sum — contradicting
    the attention-queue capability's additivity requirement.
    """
    findings = [
        audit_module.AuditFinding(
            category="prediction_window",
            severity="info",
            path="N.md",
            detail="due prediction",
            proposed_fix="x",
            meta={"review_partition": "unit-a", "signal_version": "unit-a"},
        ),
        audit_module.AuditFinding(
            category="stale_review",
            severity="warn",
            path="N.md",
            detail="possibly stale",
            proposed_fix="x",
        ),
    ]

    report = attention_module._rank(findings)

    assert [item.path for item in report.items] == ["N.md"]
    item = report.items[0]
    assert item.categories == ["prediction_window", "stale_review"]
    assert len(item.reasons) == 2
    assert item.severity == "warn"  # max over the merged reasons
    k = attention_module._RRF_K
    assert item.score == round(1.0 / (k + 1) + 1.0 / (k + 1), 6)


def test_merged_row_outranks_a_singly_flagged_one() -> None:
    """The whole point of summing: a doubly-flagged note must rise."""
    findings = [
        audit_module.AuditFinding(
            category="prediction_window",
            severity="info",
            path="both.md",
            detail="due",
            proposed_fix="x",
            meta={"review_partition": "unit-a", "signal_version": "unit-a"},
        ),
        audit_module.AuditFinding(
            category="prediction_window",
            severity="info",
            path="only.md",
            detail="due",
            proposed_fix="x",
            meta={"review_partition": "unit-b", "signal_version": "unit-b"},
        ),
        audit_module.AuditFinding(
            category="stale_review",
            severity="info",
            path="both.md",
            detail="stale",
            proposed_fix="x",
        ),
    ]

    report = attention_module._rank(findings)

    paths = [item.path for item in report.items]
    assert paths.index("both.md") < paths.index("only.md")
    assert paths.count("both.md") == 1


def test_page_signals_attach_to_each_of_several_partitioned_items() -> None:
    """N due predictions still give N triageable items, each carrying page context.

    The page-level reason must not vanish, and it must not become a phantom
    extra row either.
    """
    findings = [
        audit_module.AuditFinding(
            category="prediction_window",
            severity="info",
            path="N.md",
            detail=f"due {anchor}",
            proposed_fix="x",
            meta={"review_partition": anchor, "signal_version": anchor},
        )
        for anchor in ("unit-a", "unit-b")
    ] + [
        audit_module.AuditFinding(
            category="relation_debt",
            severity="info",
            path="N.md",
            detail="no outbound edges",
            proposed_fix="x",
        )
    ]

    report = attention_module._rank(findings)

    assert [item.path for item in report.items] == ["N.md", "N.md"]
    for item in report.items:
        assert "relation_debt" in item.categories
        assert "prediction_window" in item.categories
    partitions = {
        reason["meta"]["review_partition"]
        for item in report.items
        for reason in item.reasons
        if (reason.get("meta") or {}).get("review_partition")
    }
    assert partitions == {"unit-a", "unit-b"}


def test_due_prediction_and_page_signal_are_one_row_over_a_real_vault(
    vault: Path,
) -> None:
    """End to end: the collision the default-union promotion made routine."""
    rel = _prediction_page(
        vault, "autovacuum", _prediction_block("p1", check_by="2026-08-02")
    )

    report = attention_module.attention(
        vault, categories=["prediction_window", "relation_debt"], today=TODAY
    )

    rows = [item for item in report.items if item.path == rel]
    assert len(rows) == 1, [item.as_dict() for item in rows]
    assert set(rows[0].categories) == {"prediction_window", "relation_debt"}
    assert len({item.item_id for item in report.items}) == len(report.items)


# ---- m6: the scope guards the ADDED requirements assert with SHALL ----


def test_experiment_scope_guard_requires_the_experiment_page_type(vault: Path) -> None:
    """A non-experiment page carrying the same fields must not be surfaced."""
    _write(
        vault,
        f"{INSIGHTS}/looks-like-one.md",
        "---\ntitle: x\ntype: insight\nstatus: active\ncreated: 2025-01-01\n"
        'updated: 2025-01-01\nstarted: 2024-01-01\nduration: "30 days"\n---\n\nBody.\n',
    )

    assert _findings(vault, "unfinished_experiments") == []


def test_experiment_scope_guard_requires_a_started_date(vault: Path) -> None:
    _write(
        vault,
        f"{EXPERIMENTS}/no-start.md",
        "---\ntitle: x\ntype: experiment\ndomain: infrastructure\nstatus: active\n"
        'created: 2020-01-01\nupdated: 2020-01-01\nduration: "30 days"\nn: 1\n---\n\nBody.\n',
    )

    assert _findings(vault, "unfinished_experiments") == []


def test_experiment_scope_guard_skips_index_and_log_pages(vault: Path) -> None:
    for name in ("index.md", "log.md"):
        _write(
            vault,
            f"{EXPERIMENTS}/{name}",
            "---\ntitle: x\ntype: experiment\ndomain: infrastructure\nstatus: active\n"
            "created: 2020-01-01\nupdated: 2020-01-01\nstarted: 2020-01-01\n"
            'duration: "30 days"\nn: 1\n---\n\nBody.\n',
        )

    assert _findings(vault, "unfinished_experiments") == []


def test_experiment_scope_guard_honours_the_access_tier(vault: Path) -> None:
    _experiment(vault, "vacuum-tuning", started="2026-04-18", duration="30 days")
    _write(vault, "Knowledge Base/_access.yaml", "readonly:\n- Notes/Experiments\n")

    assert _findings(vault, "unfinished_experiments") == []


def test_prediction_scope_guard_skips_index_and_log_pages(vault: Path) -> None:
    for name in ("index.md", "log.md"):
        _write(
            vault,
            f"{INSIGHTS}/{name}",
            "---\ntitle: x\ntype: insight\nstatus: active\ncreated: 2026-01-01\n"
            "updated: 2026-01-01\n---\n\n## Prediction\n\n- id: p1\n"
            "- check_by: 2020-01-01\n\nBody.\n",
        )

    assert _findings(vault, "prediction_window") == []


def test_prediction_scope_guard_honours_the_access_tier(vault: Path) -> None:
    _prediction_page(
        vault, "autovacuum", _prediction_block("p1", check_by="2026-08-02")
    )
    _write(vault, "Knowledge Base/_access.yaml", "readonly:\n- Notes/Insights\n")

    assert _findings(vault, "prediction_window") == []


@pytest.mark.parametrize("status", ["dropped", "planned"])
def test_author_parked_statuses_generate_no_review_work(
    vault: Path, status: str
) -> None:
    """`dropped` and `planned` are inactive everywhere else in the codebase.

    A note the author explicitly dropped must not generate daily review work.
    """
    _experiment(
        vault, "parked", started="2020-01-01", duration="30 days", status=status
    )
    _prediction_page(
        vault, "parked-pred", _prediction_block("p1", check_by="2020-01-01"),
        status=status,
    )

    assert _findings(vault, "unfinished_experiments") == []
    assert _findings(vault, "prediction_window") == []


# ---- M3: the default queue must be documented at least as well as the opt-in one ----


def test_scaffold_documents_the_prediction_window_queue() -> None:
    """Every user now sees this queue unasked, so the shipped doc must describe it.

    The justification for documenting `unfinished_experiments` was that the
    scaffold had advertised it since the skill shipped. The same standard cuts
    harder here: this one arrives on the daily surface without being asked for.
    """
    doc = (
        Path(audit_module.__file__).parent
        / "_scaffold"
        / "_Schema"
        / "references"
        / "audit-checks.md"
    ).read_text(encoding="utf-8")

    entry = next(
        line for line in doc.splitlines() if "Prediction window" in line
    )
    assert "prediction_window" in entry
    assert "check_by" in entry
    assert "verdict" in entry
    assert "default" in entry
