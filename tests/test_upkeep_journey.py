"""D1-T14: the upkeep journey (plumbing for 8.5).

A fixture with link and hydration positives and negatives; the worker runs
until it is quiet; scripted agents follow what each session offers through the
real tool dispatcher.

A "session" here is what a session start may offer: the first deliverable item
of `review_memory(mode="upkeep")`, in its order. The activation carrier (T11)
and the hook render (T12) make that the REST activation read through the hook's
own fetch and render helpers; the journey's steps and assertions stay the same.

- (a) accepts the link route. The next pass marks it resolved, and a later
  session delivers the hydration item that newer facts produce.
- (b) dismisses with `false_positive:`. No later session re-delivers it, in
  either direction of the pair, until the supporting evidence changes and
  reproduces the proposal.
"""

from __future__ import annotations

import time
from pathlib import Path

import dreamer_fixture as fx
import pytest

from exomem import commands, dreamer, dreamer_families, dreamer_store, freshness, upkeep
from exomem.writer_lease import invoke_command

HOUR = 3600.0


@pytest.fixture(autouse=True)
def _clean():
    freshness.clear()
    dreamer.reset_for_tests()
    dreamer_store.clear_reader_memo()
    yield
    dreamer.reset_for_tests()
    freshness.clear()
    dreamer_store.clear_reader_memo()


def _tool(vault: Path, name: str, **kwargs):
    command = next(command for command in commands.PRODUCT_COMMANDS if command.name == name)
    return invoke_command(command, vault, **kwargs)


def _quiet(vault: Path, now: float) -> None:
    results = fx.run_to_quiet(vault, now=now)
    assert all(result.stop_reason != "error" for result in results), results


def _session(vault: Path) -> dict | None:
    """The one item a session start may offer, or None."""
    view = dreamer_store.read_view(vault)
    if view is None:
        return None
    deliverable = {row["id"] for row in upkeep.deliverable_rows(vault, view)}
    listed = _tool(vault, "review_memory", mode="upkeep")
    assert listed["status"] == "available"
    for item in listed["items"]:
        if item["ref"].rsplit("/", 1)[-1] in deliverable:
            return item
    return None


def _observed(vault: Path, *paths: str) -> None:
    """What the watcher and the graph drain do after a write lands."""
    freshness.on_files_changed(vault, changed=[vault / path for path in paths])
    fx.publish_graph(vault)


def _journey_vault(tmp_path: Path) -> Path:
    """Positives: the cavitation/inlet pair shares a source (a link).
    Negatives: the entity has newer facts from one origin only (no hydration),
    and seal wear shares no source with anything (no link)."""
    vault = fx.build(tmp_path)
    fx.edit(
        vault,
        fx.SEAL_WEAR,
        fx.insight(
            "Pump seal wear",
            sources=["field-report-two"],
            updated="2026-05-02",
            observation="Seal wear doubles after a dry start.",
        ),
    )
    return vault


def _open(vault: Path) -> list[dict]:
    view = dreamer_store.read_view(vault)
    return [row for row in view.candidates if row["state"] == "open"]


def test_a_accepting_the_link_resolves_it_and_a_later_session_offers_hydration(
    tmp_path: Path,
) -> None:
    vault = _journey_vault(tmp_path)
    start = time.time()
    _quiet(vault, start)
    assert {row["family"] for row in _open(vault)} == {dreamer_families.LINK_FAMILY}
    assert {row["subject_path"] for row in _open(vault)} == {fx.CAVITATION, fx.INLET}
    # Nothing is offered until the evidence has settled.
    assert _session(vault) is None
    _quiet(vault, start + 2 * HOUR)

    item = _session(vault)
    assert item is not None and item["family"] == dreamer_families.LINK_FAMILY
    assert item["route"]["args"]["path"] == fx.CAVITATION
    context = _tool(vault, item["context_route"]["tool"], **item["context_route"]["args"])
    assert context["mutated"] is False
    accepted = _tool(
        vault,
        item["route"]["tool"],
        **item["route"]["args"],
        why="both notes come from the same field report",
        expected_hash=context["subject"]["content_hash"],
    )
    assert accepted["status"] in {"applied", "committed", "accepted"}, accepted
    _observed(vault, fx.CAVITATION)

    # The next pass marks the pair resolved; nothing else is left to offer.
    _quiet(vault, start + 2 * HOUR + 60)
    assert _open(vault) == []
    assert _session(vault) is None

    # Newer facts from a second origin: hydration appears, and is offered once settled.
    fx.write(
        vault,
        fx.SEAL_WEAR,
        fx.insight(
            "Pump seal wear",
            sources=["field-report-two"],
            updated="2026-05-02",
            links="Seal wear on the [[Notes/Entities/orbit-pump]] doubles after a dry start.",
            observation="Seal wear doubles after a dry start.",
        ),
    )
    _observed(vault, fx.SEAL_WEAR)
    _quiet(vault, start + 3 * HOUR)
    assert _session(vault) is None
    _quiet(vault, start + 5 * HOUR)
    item = _session(vault)
    assert item is not None and item["family"] == dreamer_families.HYDRATION_FAMILY
    assert item["route"]["args"]["paths"][0] == fx.ENTITY


def test_b_a_false_positive_is_held_until_the_evidence_changes(tmp_path: Path) -> None:
    vault = _journey_vault(tmp_path)
    start = time.time()
    _quiet(vault, start)
    _quiet(vault, start + 2 * HOUR)
    item = _session(vault)
    assert item is not None and item["family"] == dreamer_families.LINK_FAMILY
    first_fingerprint = item["fingerprint"]

    dismissed = _tool(
        vault,
        item["dispose"]["tool"],
        **item["dispose"]["args"],
        action="dismiss",
        why="false_positive: the shared report covers unrelated runs",
    )
    assert dismissed["state"] == "dismissed"
    assert _session(vault) is None

    # Later sessions, with passes in between and a same-bytes rewrite of both
    # endpoints: neither direction of the pair comes back.
    for step, touch in enumerate((False, True, False), start=1):
        if touch:
            for rel in (fx.CAVITATION, fx.INLET):
                fx.write(vault, rel, (vault / rel).read_text("utf-8"))
            _observed(vault, fx.CAVITATION, fx.INLET)
        _quiet(vault, start + (2 + 2 * step) * HOUR)
        assert _session(vault) is None, step

    # The supporting evidence changes: both notes now cite another report.
    for rel, title, observation in (
        (fx.CAVITATION, "Pump cavitation", "Cavitation starts above 40 litres a minute."),
        (fx.INLET, "Pump inlet pressure", "Inlet pressure falls before cavitation begins."),
    ):
        fx.write(
            vault,
            rel,
            fx.insight(
                title, sources=["field-report-three"], updated="2026-05-06", observation=observation
            ),
        )
    _observed(vault, fx.CAVITATION, fx.INLET)
    _quiet(vault, start + 10 * HOUR)
    assert _session(vault) is None  # not settled yet
    _quiet(vault, start + 12 * HOUR)
    again = _session(vault)
    assert again is not None and again["family"] == dreamer_families.LINK_FAMILY
    assert again["ref"] == item["ref"]
    assert again["fingerprint"] != first_fingerprint


def test_the_hook_line_names_a_ref_its_own_advice_accepts(tmp_path: Path) -> None:
    """The session-start line says "Review with review_item_context" and ends in
    a ref: that ref must resolve there, and dispose of the item through triage."""
    import re

    from exomem._hooks import exomem_retrieve_nudge as hook

    vault = _journey_vault(tmp_path)
    start = time.time()
    _quiet(vault, start)
    _quiet(vault, start + 2 * HOUR)
    item = _session(vault)
    assert item is not None and item["family"] == dreamer_families.LINK_FAMILY
    (line,) = hook._upkeep_lines({"upkeep": {"items": [item]}})
    assert "review_item_context" in line
    rendered = re.search(r"\[([^\[\]]+)\]$", line).group(1)
    context = _tool(vault, "review_item_context", ref=rendered)
    assert context["mutated"] is False
    triaged = _tool(
        vault,
        "triage_memory",
        ref=rendered,
        action="dismiss",
        why="false_positive: unrelated notes",
        expected_fingerprint=item["fingerprint"],
    )
    assert triaged["family"] == dreamer_families.LINK_FAMILY, triaged


def test_a_dismissed_pair_stays_held_when_the_row_cap_evicts_its_offered_direction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dismissing the offered direction of a link pair holds the pair, even
    after the row cap evicts that direction: its twin is never offered."""
    vault = _journey_vault(tmp_path)
    start = time.time()
    _quiet(vault, start)
    _quiet(vault, start + 2 * HOUR)
    item = _session(vault)
    assert item is not None and item["family"] == dreamer_families.LINK_FAMILY
    _tool(
        vault,
        "triage_memory",
        ref=item["context_route"]["args"]["ref"],
        action="dismiss",
        why="false_positive: unrelated notes",
        expected_fingerprint=item["fingerprint"],
    )
    _quiet(vault, start + 3 * HOUR)
    links = [row for row in _open(vault) if row["family"] == dreamer_families.LINK_FAMILY]
    assert {row["subject_path"] for row in links} == {fx.CAVITATION, fx.INLET}
    # The twin of a dismissed row is parked too: it holds no family-cap slot.
    ctx = dreamer_families.Context(
        vault_root=vault,
        store=dreamer_store.DreamerStore(vault),
        conn=dreamer_store.DreamerStore(vault).connect(),
        now=start + 3 * HOUR,
    )
    try:
        assert all(ctx.parked(str(row["id"]), str(row["fingerprint"])) for row in links)
    finally:
        ctx.conn.close()
        ctx.close()

    # One more proposal elsewhere reaches the row cap.
    monkeypatch.setattr(
        dreamer_store, "MAX_ROWS", len(dreamer_store.read_view(vault).candidates) + 1
    )
    for rel, title in (
        (f"{fx.KB}/Notes/Insights/valve-a.md", "Valve a"),
        (f"{fx.KB}/Notes/Insights/valve-b.md", "Valve b"),
    ):
        fx.edit(
            vault,
            rel,
            fx.insight(
                title,
                sources=["field-report-three"],
                updated="2026-05-04",
                observation=f"{title} sticks when cold.",
            ),
        )
    _quiet(vault, start + 4 * HOUR)
    _quiet(vault, start + 6 * HOUR)
    view = dreamer_store.read_view(vault)
    offered = {
        row["subject_path"]
        for row in upkeep.deliverable_rows(vault, view)
        if row["family"] == dreamer_families.LINK_FAMILY
    }
    assert not offered & {fx.CAVITATION, fx.INLET}, offered
    # The new pair is still offered: the hold is the dismissed pair's alone.
    assert offered, "the fresh pair was not offered"


def test_a_stale_decision_on_an_evicted_twin_holds_only_until_it_is_proposed_again(
    tmp_path: Path,
) -> None:
    """A direction dismissed at one fingerprint, re-proposed at another, then
    evicted: its twin is held (conservatively) only until that direction's
    page is proposed again and judged on its current fingerprint."""
    vault = _journey_vault(tmp_path)
    start = time.time()
    _quiet(vault, start)
    _quiet(vault, start + 2 * HOUR)
    item = _session(vault)
    assert item is not None and item["family"] == dreamer_families.LINK_FAMILY
    offered_path = item["route"]["args"]["path"]
    _tool(
        vault,
        "triage_memory",
        ref=item["context_route"]["args"]["ref"],
        action="dismiss",
        why="false_positive: unrelated notes",
        expected_fingerprint=item["fingerprint"],
    )
    # The evidence moves: the dismissed direction is proposed again at a new
    # fingerprint the dismissal does not bind.
    fx.edit(
        vault,
        offered_path,
        fx.insight(
            "Pump cavitation",
            sources=["field-report-one"],
            updated="2026-06-01",
            observation="Cavitation now appears below 2 bar.",
        ),
    )
    _quiet(vault, start + 3 * HOUR)
    current = next(
        row
        for row in _open(vault)
        if row["family"] == dreamer_families.LINK_FAMILY and row["subject_path"] == offered_path
    )
    assert current["fingerprint"] != item["fingerprint"]
    # Its current row is evicted, as the family cap evicts the oldest eligible row.
    store = dreamer_store.DreamerStore(vault)
    conn = store.connect()
    try:
        with store.write(conn):
            store._delete(conn, [current["id"]])
    finally:
        conn.close()
    dreamer_store.clear_reader_memo()

    # The twin is held by the stale decision, and the evicted direction's page
    # is queued once for that page version, never again while it is unchanged.
    twin = next(
        row
        for row in _open(vault)
        if row["family"] == dreamer_families.LINK_FAMILY and row["subject_path"] != offered_path
    )

    def queued_after_hold() -> bool:
        conn = store.connect()
        ctx = dreamer_families.Context(vault_root=vault, store=store, conn=conn, now=start)
        try:
            with store.write(conn):
                assert ctx.pair_held(twin)
                queued = conn.execute(
                    "SELECT 1 FROM pending WHERE path=?", (offered_path,)
                ).fetchone()
                conn.execute("DELETE FROM pending WHERE path=?", (offered_path,))
        finally:
            ctx.close()
            conn.close()
        return queued is not None

    assert queued_after_hold()
    assert not queued_after_hold()
    # Put it back as a pass that saw the hold would have left it.
    conn = store.connect()
    try:
        with store.write(conn):
            store.pending_add(conn, [offered_path])
    finally:
        conn.close()

    # The next pass queues that direction's page, the one after proposes it
    # again, and it is offered once settled.
    _quiet(vault, start + 6 * HOUR)
    _quiet(vault, start + 7 * HOUR)
    _quiet(vault, start + 9 * HOUR)
    again = next(
        (
            row
            for row in _open(vault)
            if row["family"] == dreamer_families.LINK_FAMILY and row["subject_path"] == offered_path
        ),
        None,
    )
    assert again is not None and again["fingerprint"] == current["fingerprint"], again
    view = dreamer_store.read_view(vault)
    offered = {row["subject_path"] for row in upkeep.deliverable_rows(vault, view)}
    assert offered_path in offered, offered
