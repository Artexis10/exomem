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
