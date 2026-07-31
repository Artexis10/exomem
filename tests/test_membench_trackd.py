"""Track D workflow-journey tests: J1 and J2 run green end-to-end against
fresh isolated vaults, and a deliberate wrong-order J1 variant (final replace
skipped) fails its chain checks — proving the checks bite.

Vaults are per-test ``tmp_path`` children; ``journey_env`` pins
EXOMEM_VAULT_PATH + the lexical/deterministic profile for every subprocess.
"""

from __future__ import annotations

import json
from pathlib import Path

from membench.judge import load_requests
from membench.trackd.journeys import (
    J3_RUBRIC_PATH,
    PlantedItem,
    QueueObservation,
    load_j3_rubric,
    run_j1_longitudinal,
    run_j2_correction,
    run_j3_weekly_review,
    score_review_queue,
    write_j3_judge_requests,
)
from membench.trackd.runner import JOURNEYS


def test_j1_longitudinal_evolution_green(tmp_path: Path) -> None:
    result = run_j1_longitudinal(tmp_path / "j1")
    assert result.ok, f"failed checks: {result.failed}\n" + "\n".join(
        f"{c.name}: {c.detail}" for c in result.checks if not c.ok
    )
    assert result.manual_interventions == 0
    # Journey shape: init + 3 writes + evolution + ask + 2 reads = 8 CLI steps.
    assert result.steps_count == 8
    expected_checks = {
        "init vault",
        "remember v1 commits",
        "replace to v2 commits",
        "replace to v3 commits",
        "evolution shows 3-state chain in order",
        "evolution anchors: chain_id=newest, topic_anchor=oldest",
        "ask returns the current (v3-value) page first",
        "top hit carries the current value",
        "superseded v1 remains readable",
        "superseded v2 remains readable",
        "page-count discipline (no duplicate sprawl)",
    }
    assert expected_checks == {c.name for c in result.checks}
    report = result.as_dict()
    assert report["ok"] is True and report["checks_failed"] == []


def test_j2_correction_propagation_green(tmp_path: Path) -> None:
    result = run_j2_correction(tmp_path / "j2")
    assert result.ok, f"failed checks: {result.failed}\n" + "\n".join(
        f"{c.name}: {c.detail}" for c in result.checks if not c.ok
    )
    assert result.manual_interventions == 0
    # init + capture + remember + replace + 5 asks + audit + 2 reads = 12 steps.
    assert result.steps_count == 12
    paraphrase_checks = [c for c in result.checks if c.name.startswith("paraphrase ")]
    assert len(paraphrase_checks) == 5 and all(c.ok for c in paraphrase_checks)
    assert any(
        c.name == "corrected page retains source provenance in sources: frontmatter"
        for c in result.checks
    )


def test_j1_wrong_order_variant_fails_chain_check(tmp_path: Path) -> None:
    """Skipping the v2->v3 replace must break the declared 3-state chain."""
    result = run_j1_longitudinal(tmp_path / "j1-skip", skip_final_replace=True)
    assert not result.ok
    assert "evolution shows 3-state chain in order" in result.failed
    assert "ask returns the current (v3-value) page first" in result.failed
    # The vault still holds only the two written versions (discipline check
    # itself passes for 2 pages — the failure is the missing third state).
    page_check = next(
        c for c in result.checks if c.name == "page-count discipline (no duplicate sprawl)"
    )
    assert page_check.ok, page_check.detail


def test_registry_exposes_all_journeys() -> None:
    assert set(JOURNEYS) == {"j1_longitudinal", "j2_correction", "j3_weekly_review"}


# ------------------------------------------------------------------- J3

_PLANTED = [
    PlantedItem("p-stale", "stale", "notes/dormant-conclusion.md"),
    PlantedItem("p-contra-a", "contradiction", "notes/cap-a.md"),
    PlantedItem("p-contra-b", "contradiction", "notes/cap-b.md"),
    PlantedItem("p-unproc-a", "unprocessed", "sources/raw-one.md"),
    PlantedItem("p-unproc-b", "unprocessed", "sources/raw-two.md"),
    PlantedItem("p-open", "open_loop", "notes/bootstrap-conclusion.md"),
]


def test_j3_planted_queue_scoring_golden() -> None:
    """Known planted ids -> recall/precision; a surfacing decoy hurts precision."""

    clean = score_review_queue(
        _PLANTED,
        QueueObservation("stale", ("notes/dormant-conclusion.md",)),
        expected_kinds=("stale",),
    )
    assert clean["recall"] == 1.0
    assert clean["precision"] == 1.0
    assert clean["false_surface_rate"] == 0.0
    assert clean["false_surfaces"] == []

    with_decoy = score_review_queue(
        _PLANTED,
        QueueObservation(
            "unprocessed-sources",
            ("sources/raw-one.md", "sources/raw-two.md", "sources/decoy.md"),
        ),
        expected_kinds=("unprocessed",),
    )
    assert with_decoy["recall"] == 1.0
    assert with_decoy["precision"] == 2 / 3
    assert with_decoy["false_surfaces"] == ["sources/decoy.md"]
    assert with_decoy["false_surface_rate"] == 1 / 3

    missed = score_review_queue(
        _PLANTED,
        QueueObservation("stale", ()),
        expected_kinds=("stale",),
    )
    assert missed["recall"] == 0.0
    assert missed["precision"] is None  # nothing surfaced: no precision claim
    assert missed["false_surface_rate"] is None


def test_j3_unsupported_queue_is_never_scored_zero() -> None:
    unsupported = score_review_queue(
        _PLANTED,
        QueueObservation("contradiction", (), supported=False),
        expected_kinds=("contradiction",),
    )
    assert unsupported["supported"] is False
    assert unsupported["recall"] is None
    assert unsupported["precision"] is None
    assert unsupported["false_surface_rate"] is None


def test_j3_rubric_schema_is_complete() -> None:
    assert J3_RUBRIC_PATH.is_file(), f"missing rubric: {J3_RUBRIC_PATH}"
    rubric = load_j3_rubric()
    assert rubric["journey"] == "j3_weekly_review"
    assert rubric["pairing"] == "blind"
    assert rubric["order"] == "randomized"
    assert isinstance(rubric["samples"], int) and rubric["samples"] >= 1
    assert rubric["criteria"], "criteria must be non-empty"
    for criterion in rubric["criteria"]:
        assert criterion["id"] and criterion["question"]
        anchors = criterion["anchors"]
        assert set(anchors) == {"1", "2", "3", "4", "5"}
        assert all(isinstance(v, str) and v for v in anchors.values())


def test_j3_judge_wiring_routes_summary_through_handshake(tmp_path: Path) -> None:
    summaries = {
        "provider-one": (
            "Weekly review: the dormant-conclusion queue surfaced 1 of 1 planted "
            "items with 0 false surfaces; raw-capture backlog surfaced 2 of 2."
        ),
        "provider-two": (
            "Weekly review: the dormant-conclusion queue surfaced 0 of 1 planted "
            "items; raw-capture backlog surfaced 2 of 2 with 1 false surface."
        ),
    }
    batch = write_j3_judge_requests(tmp_path, summaries, seed="j3-test")
    assert batch.is_file()
    rubric = load_j3_rubric()
    requests = load_requests(tmp_path, "judge")
    assert len(requests) == len(rubric["criteria"]) * len(summaries) * rubric["samples"]
    for request in requests:
        assert request.blinded_provider_token.startswith("system-")
        payload = request.payload
        assert payload["journey"] == "j3_weekly_review"
        assert payload["criterion_id"] in {c["id"] for c in rubric["criteria"]}
        assert payload["prompt"], "judge prompt must be present"
        line = json.dumps(payload)
        assert "provider-one" not in line and "provider-two" not in line


def test_j3_weekly_review_live_green(tmp_path: Path) -> None:
    """Live J3 against a fresh isolated vault (~13 CLI steps, well under 60s)."""

    result = run_j3_weekly_review(tmp_path / "j3")
    assert result.ok, f"failed checks: {result.failed}\n" + "\n".join(
        f"{c.name}: {c.detail}" for c in result.checks if not c.ok
    )
    assert result.manual_interventions == 0
    # init + 3 captures + 5 notes + 4 review queues = 13 scripted CLI steps.
    assert result.steps_count == 13
    names = {c.name for c in result.checks}
    assert "stale queue surfaces exactly the planted dormant conclusion" in names
    assert "unprocessed queue surfaces exactly the planted raw sources" in names
    assert "contradiction queue honestly unsupported in the lexical profile" in names
    assert "attention queue covers every surfaceable open loop" in names
    assert "triage burden equals the scripted op count" in names

    # Judge-facing summary rides on the result and stays path/product free.
    assert result.summary_text
    assert "Knowledge Base" not in result.summary_text
    assert ".md" not in result.summary_text

    report = result.as_dict()
    assert report["ok"] is True
    assert report["summary_text"] == result.summary_text
    assert {q["mode"] for q in report["queue_scores"]} == {
        "stale",
        "contradiction",
        "unprocessed-sources",
        "attention",
    }
    contradiction = next(
        q for q in report["queue_scores"] if q["mode"] == "contradiction"
    )
    assert contradiction["supported"] is False  # embeddings-gated sweep
    assert contradiction["recall"] is None  # unsupported is never zero
