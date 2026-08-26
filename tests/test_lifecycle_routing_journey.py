"""One turn-by-turn journey over the whole route: intent, outcome, consequence.

Every step is a real product call on a real vault. Nothing here stubs the audit,
the projection, the carrier or the review store, because the claim under test is
that the pieces compose — each of them already passes on its own.
"""

from __future__ import annotations

import sys
from pathlib import Path

from lifecycle_fixtures import (
    PLANNING_PATH,
    RECORDS_PATH,
    UNBOUND_PATH,
    queue_item,
    report_event,
    seed_vault,
    settle_item,
)

from exomem import audit as audit_module
from exomem import due_state as due_state_module
from exomem import review_state as review_state_module
from exomem.commands import op_triage_memory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks"))

FAMILY = "unreflected_outcomes"
QUEUED = ("Batch 1", "Batch 2", "Batch 3", "Batch 4")


def _open_titles(vault_root: Path) -> set[str]:
    report = audit_module.audit(vault_root, categories=[FAMILY])
    return {str((finding.meta or {}).get("plan_title")) for finding in report.findings}


def _served_refs(vault_root: Path) -> list[str]:
    return [row["ref"] for row in due_state_module.served_entries(vault_root)]


def _dismissals(vault_root: Path) -> dict:
    return review_state_module.ReviewStateStore(vault_root).load()["records"]


def test_the_lifecycle_route_from_queued_intent_to_reported_consequence(
    tmp_path: Path,
) -> None:
    due_state_module.reset_emission_state()
    seed_vault(tmp_path, unbound=True)
    added = {title: queue_item(tmp_path, title) for title in QUEUED}
    due_state_module.reconcile(tmp_path)
    assert _open_titles(tmp_path) == set(), "queued intent alone is not a consequence"

    # 1. Three deliverables are reported produced. The third append is the one
    #    that reports the state it just created, in its own response.
    report_event(tmp_path, "Batch 1")
    report_event(tmp_path, "Batch 2")
    third = report_event(tmp_path, "Batch 3")

    assert _open_titles(tmp_path) == {"Batch 1", "Batch 2", "Batch 3"}
    assert third["due_state"]["total"] == 3
    assert third["due_state"]["categories"] == {FAMILY: 3}

    # 2. The same outcome stated twice is one event, not two: the identity comes
    #    from the collection's own declared natural key.
    restated = report_event(tmp_path, "Batch 3")
    assert restated["outcome"] == "replayed"
    assert restated["item_key"] == third["item_key"]

    # 3. A twin collection with no binding says nothing, however many events land.
    report_event(tmp_path, "Batch 4", collection=UNBOUND_PATH)
    assert _open_titles(tmp_path) == {"Batch 1", "Batch 2", "Batch 3"}

    # 4. The decider moves the three items. The findings clear because the state
    #    changed -- no dismissal is recorded, and none is needed.
    for title in ("Batch 1", "Batch 2", "Batch 3"):
        settle_item(tmp_path, added[title])
    assert _open_titles(tmp_path) == set()
    assert _served_refs(tmp_path) == []
    assert _dismissals(tmp_path) == {}

    # 5. A fourth deliverable is reported produced and the reader dismisses it.
    report_event(tmp_path, "Batch 4")
    assert _open_titles(tmp_path) == {"Batch 4"}
    due_state_module.reconcile(tmp_path)
    surfaced = due_state_module.served_entries(tmp_path)
    assert len(surfaced) == 1
    first_fingerprint = surfaced[0]["fingerprint"]
    op_triage_memory(
        tmp_path,
        ref=surfaced[0]["ref"],
        action="dismiss",
        why="intentional: this one ships next week",
        expected_fingerprint=first_fingerprint,
    )

    # 6. The dismissal holds across a re-run: nothing resurfaces on its own.
    due_state_module.reconcile(tmp_path)
    assert _served_refs(tmp_path) == []
    assert _open_titles(tmp_path) == {"Batch 4"}, "measured, not clean"

    # 7. A second event on the same item is materially new, so it comes back --
    #    and the dismissal record stands rather than being rewritten.
    report_event(tmp_path, "Batch 4", occurred_on="2026-08-09")
    due_state_module.reconcile(tmp_path)
    resurfaced = due_state_module.served_entries(tmp_path)
    assert len(resurfaced) == 1
    assert resurfaced[0]["fingerprint"] != first_fingerprint
    assert _dismissals(tmp_path), "the earlier decision is history, not state to erase"

    # 8. The durable state both collections actually hold, read by the neutral
    #    benchmark projector rather than by the code under test.
    from epistemic.projectors.exomem_vault import VaultProjector

    snapshot = VaultProjector(tmp_path).project(phase="end", taken_at="2026-08-20T00:00:00Z")
    sections = {section.manifest: section for section in snapshot.collections}
    planning = sections[PLANNING_PATH]
    statuses = {
        item.natural_key["title"]: item.status
        for item in planning.items
        if item.natural_key.get("title") in QUEUED
    }
    assert statuses == {
        "Batch 1": "completed",
        "Batch 2": "completed",
        "Batch 3": "completed",
        "Batch 4": "planned",
    }
    events = sections[RECORDS_PATH]
    # Five events, not six: the re-stated one replayed onto its own identity.
    assert sorted(item.natural_key["title"] for item in events.items) == [
        "Batch 1",
        "Batch 2",
        "Batch 3",
        "Batch 4",
        "Batch 4",
    ]
    assert [item.natural_key["title"] for item in sections[UNBOUND_PATH].items] == ["Batch 4"]
    # The runtime never moved anything: every completed item was moved by the
    # triage call in step 3, and the one nobody triaged is still planned.
    assert all(item.lifecycle == "active" for item in planning.items)
