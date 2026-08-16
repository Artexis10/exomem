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

Both categories are registered for opt-in `attention` selection and deliberately
absent from the default union, so a grandfathered corpus cannot flood the daily
review surface on upgrade.
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


def test_default_attention_union_is_unchanged() -> None:
    assert attention_module.DEFAULT_ATTENTION_CATEGORIES == (
        "bridge_review",
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


def test_prediction_window_is_selectable_but_not_default_in_attention(
    vault: Path,
) -> None:
    rel = _prediction_page(
        vault, "autovacuum", _prediction_block("p1", check_by="2026-08-02")
    )

    assert "prediction_window" in attention_module.ATTENTION_CATEGORIES
    assert "prediction_window" not in attention_module.DEFAULT_ATTENTION_CATEGORIES

    selected = attention_module.attention(
        vault, categories=["prediction_window"], today=TODAY
    )
    assert [item.path for item in selected.items] == [rel]

    default = attention_module.attention(vault, today=TODAY)
    assert all("prediction_window" not in item.categories for item in default.items)
