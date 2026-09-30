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

from exomem import working_set

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
) -> working_set.LaneItem:
    return working_set.LaneItem(
        role=role,
        level=level,
        ref=ref,
        path=path,
        title=path.rsplit("/", 1)[-1].removesuffix(".md"),
        text=text,
        lifecycle="active",
        updated="2026-09-01",
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
