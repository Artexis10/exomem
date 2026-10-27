"""A unit the anchor page's own lede already says is served once (close-memory-loop,
activation quality).

The identity lane serves a resolved anchor's page in its own words, by the
page's own ref. A role lane reading the same page can select the observation
that lede opens with, as a unit fragment of that page. Served both ways it is
one sentence printed twice, charged twice against the budget, and reported as
two references where there is one page. The page-level item already carries it,
so the fragment is dropped; any other unit of that page, and the same sentence
on another page, are untouched. Invented content only.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from exomem import commands, lexstore, working_set, working_set_index, working_set_runtime

KILN = "Knowledge Base/Systems/Glaze kiln.md"
OTHER = "Knowledge Base/Notes/Studio/firing-log.md"


def _item(
    *,
    role: str,
    level: str,
    ref: str,
    path: str,
    text: str,
    source: str = "units",
    updated: str = "",
) -> working_set.LaneItem:
    return working_set.LaneItem(
        role=role,
        level=level,
        ref=ref,
        path=path,
        title=path.rsplit("/", 1)[-1].removesuffix(".md"),
        text=text,
        lifecycle="active",
        updated=updated,
        anchor=KILN,
        provenance={"source": source},
    )


LEDE = _item(
    role="identity",
    level="page",
    ref=KILN,
    path=KILN,
    text="[resource] The glaze kiln fires up to cone six. #resource #studio",
    source="profile",
    updated="2026-09-01",
)
REPEAT = _item(
    role="resources",
    level="unit",
    ref="exomem://vault/Knowledge%20Base/Systems/Glaze%20kiln.md#unit-aaaa",
    path=KILN,
    text="The glaze kiln fires up to cone six.",
)
OWN = _item(
    role="constraints",
    level="unit",
    ref="exomem://vault/Knowledge%20Base/Systems/Glaze%20kiln.md#unit-bbbb",
    path=KILN,
    text="Book the kiln two days ahead of a firing.",
)
ELSEWHERE = _item(
    role="precedents",
    level="unit",
    ref="exomem://vault/Knowledge%20Base/Notes/Studio/firing-log.md#unit-cccc",
    path=OTHER,
    text="The glaze kiln fires up to cone six.",
)


def _packet(*items: working_set.LaneItem) -> dict:
    roles = [{"id": role, "source": "anchor_default", "lane": "units"} for role in (
        "identity",
        "resources",
        "constraints",
        "precedents",
    )]
    return working_set.build_packet(
        items=items,
        anchors=({"ref": KILN, "path": KILN, "status": "resolved", "kind": "resource"},),
        roles=roles,
        current_state=(),
        ambiguity=(),
        missing=(),
        max_chars=4000,
        generation={},
        status="resolved",
    )


def test_a_unit_the_anchor_lede_already_says_is_served_once() -> None:
    packet = _packet(LEDE, REPEAT, OWN, ELSEWHERE)
    refs = [unit["ref"] for unit in packet["units"]]

    assert REPEAT.ref not in refs
    assert refs.count(KILN) == 1
    assert OWN.ref in refs
    assert ELSEWHERE.ref in refs
    assert packet["budget"]["used_chars"] == sum(len(unit["text"]) for unit in packet["units"])


def test_without_the_lede_the_unit_is_served() -> None:
    packet = _packet(REPEAT, OWN)

    assert {unit["ref"] for unit in packet["units"]} == {REPEAT.ref, OWN.ref}


@pytest.mark.parametrize("qualification", [
    {"provenance": {"verdict": "refuted"}},
    {"provenance": {"check_by": "2027-01-15"}},
    {"updated": "2026-08-30"},
    {"lifecycle": "superseded"},
    {"provenance": {"corrects": OWN.ref, "relation": "refines", "family": "refinement"}},
    {"provenance": {"supersedes_targets": [OWN.ref]}},
    {"provenance": {"superseded_by": [OTHER]}},
], ids=["verdict", "review-date", "authored-date", "lifecycle", "correction", "successor", "replaced"])
def test_a_repeated_unit_keeps_its_own_qualifications(qualification: dict) -> None:
    """Page prose cannot replace the unit that carries a distinct qualification."""
    qualified = replace(REPEAT, **qualification)

    packet = _packet(LEDE, qualified)

    served = next(unit for unit in packet["units"] if unit["ref"] == qualified.ref)
    assert served["updated"] == qualified.updated
    assert served["lifecycle"] == qualified.lifecycle


@pytest.mark.usefixtures("anchors_packet")  # tests the rollback packet; maps is the default
def test_activation_keeps_a_repeated_claims_verdict_and_own_date(vault: Path) -> None:
    """Repeating a judged claim in the introduction must not erase its qualification."""
    page = vault / KILN
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(
        "---\ntype: resource\nstatus: active\nupdated: 2026-10-01\n"
        "exomem_id: 11111111-1111-4111-8111-111111111111\n---\n\n"
        "# Glaze kiln\n\nThe glaze kiln fires up to cone six.\n\n"
        "## Claim\n- category: fact\n- id: capacity\n"
        "- context: observed 2026-09-03\n- verdict: refuted\n"
        "- check_by: 2027-01-15\n\nThe glaze kiln fires up to cone six.\n",
        encoding="utf-8",
    )
    lexstore.ensure_fresh(vault)
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(vault).rebuild()
    reference = "exomem://memory/11111111-1111-4111-8111-111111111111#capacity"
    exact = commands.op_read_memory(vault, path=KILN, unit_ref=reference)

    packet = commands.op_activate_context(
        vault, turn="Remind me about the glaze kiln.", max_chars=4000,
    )

    claim = next(unit for unit in packet["units"] if unit["ref"] == reference)
    assert claim["provenance"]["verdict"] == "refuted"
    assert claim["provenance"]["check_by"] == "2027-01-15"
    assert claim["provenance"]["source_hash"] == exact["unit"]["source_hash"]
    assert claim["updated"] == "2026-09-03"
    assert claim["lifecycle"] == "active"
    assert packet["budget"]["used_chars"] <= 4000
    from exomem._hooks import exomem_retrieve_nudge as hook

    rendered = "\n".join(hook._packet_lines(packet))
    assert "verdict=refuted check_by=2027-01-15" in rendered
    assert reference in rendered
