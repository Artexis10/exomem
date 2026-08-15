"""Track D workflow-journey tests: J1 and J2 run green end-to-end against
fresh isolated vaults, and a deliberate wrong-order J1 variant (final replace
skipped) fails its chain checks — proving the checks bite.

Vaults are per-test ``tmp_path`` children; ``journey_env`` pins
EXOMEM_VAULT_PATH + the lexical/deterministic profile for every subprocess.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from membench.judge import load_requests
from membench.trackd.journeys import (
    J3_RUBRIC_PATH,
    PlantedItem,
    QueueObservation,
    journey_env,
    load_j3_rubric,
    normalize_instant,
    run_j1_longitudinal,
    run_j2_correction,
    run_j3_weekly_review,
    score_review_queue,
    write_j3_judge_requests,
)
from membench.trackd.runner import JOURNEYS, run_journeys


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
        "evolution anchors: chain_id=head, topic_anchor=requested path",
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


@pytest.mark.timeout(180)
def test_pinned_clock_keeps_track_d_verdicts_stable_across_utc_midnight(tmp_path: Path) -> None:
    """Date-named artifacts must not alter a journey verdict at midnight (4b.34)."""

    before_midnight = dt.datetime(2026, 8, 9, 23, 59, tzinfo=dt.UTC)
    after_midnight = dt.datetime(2026, 8, 10, 0, 1, tzinfo=dt.UTC)
    before_root = tmp_path / "before"
    after_root = tmp_path / "after"
    before_root.mkdir()
    after_root.mkdir()
    before = run_journeys(
        ("j3_weekly_review",), tmp_root=before_root, instant=before_midnight
    )
    after = run_journeys(
        ("j3_weekly_review",), tmp_root=after_root, instant=after_midnight
    )

    assert before["summary"] == after["summary"]
    assert [
        (journey["id"], journey["ok"], journey["checks_failed"])
        for journey in before["journeys"]
    ] == [
        (journey["id"], journey["ok"], journey["checks_failed"])
        for journey in after["journeys"]
    ]


@pytest.mark.timeout(180)
def test_pinned_clock_controls_the_local_day_of_produced_note_paths(tmp_path: Path) -> None:
    """A broken subprocess hook must not silently fall back to wall-clock time."""

    instant = dt.datetime(2022, 1, 2, 1, 2, tzinfo=dt.UTC)
    workdir = tmp_path / "pinned-day"
    env = journey_env(workdir / "vault", workdir, instant)
    env.pop("EXOMEM_TRACKD_INSTANT")
    probe = subprocess.run(
        [sys.executable, "-c", "pass"],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
    )
    assert probe.returncode != 0
    assert "EXOMEM_TRACKD_INSTANT is required by Track D" in probe.stderr

    result = run_j2_correction(workdir, instant=instant)

    assert result.ok, result.failed
    local_day = instant.astimezone().date().isoformat()
    note_paths = [
        path.name
        for path in (workdir / "vault" / "Knowledge Base" / "Sources").rglob("*.md")
        if path.name != "index.md"
    ]
    assert any(name.startswith(local_day) for name in note_paths), note_paths


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="platform lacks local-zone control")
def test_pinned_clock_preserves_temporal_now_local_zone(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the instant, not a UTC conversion that changes render_date's day."""

    previous_tz = os.environ.get("TZ")
    monkeypatch.setenv("TZ", "Europe/Tallinn")
    time.tzset()
    try:
        instant = dt.datetime(2022, 1, 2, 22, 30, tzinfo=dt.UTC)
        normalized = normalize_instant(instant)
        assert normalized.date() == dt.date(2022, 1, 3)
        assert normalized.utcoffset() == dt.timedelta(hours=2)
    finally:
        if previous_tz is None:
            monkeypatch.delenv("TZ", raising=False)
        else:
            monkeypatch.setenv("TZ", previous_tz)
        time.tzset()


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
    assert "contradiction queue surfaces exactly the planted authored pair" in names
    assert "contradiction row names both endpoints of the planted pair" in names
    assert (
        "contradiction proximity lane declared unsupported, not scored zero" in names
    )
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
    # The asserted (authored-edge) lane is deterministic and runs without
    # embeddings, so it is measured; the proximity lane is still gated and must
    # stay declared-unsupported rather than folded into that recall.
    assert contradiction["supported"] is True
    assert contradiction["recall"] == 1.0
    assert contradiction["unsupported_lanes"] == ["proximity"]
    # One row per pair, anchored on the lower path — not one row per endpoint.
    assert len(contradiction["expected"]) == 1
