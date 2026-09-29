"""A resolved entity's own conclusions and its project's standing precedent.

A turn resolved a person entity. Activation left out (a) that person's
topic-specific conclusion, which links to the entity but shares almost no word
with the turn and sat past the entity's capped link list, and (b) the project's
standing methodology page, which constrains how any claim in the domain is
argued. The agent then fell back to defaults and re-opened settled method. And a
served scoped claim was cut before its qualifier.

Invented names throughout.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import (
    lexstore,
    working_set,
    working_set_index,
    working_set_resolve,
    working_set_runtime,
)

ENTITY = "Knowledge Base/Entities/People/Ilse Vandermeer.md"
CONCLUSION = "Knowledge Base/Notes/Decisions/zz-tide-panel-priority-subset.md"
STANDING = "Knowledge Base/Notes/Patterns/harbor-study-operating-method.md"
OTHER_STANDING = "Knowledge Base/Notes/Patterns/harbor-study-older-method.md"
FOREIGN_STANDING = "Knowledge Base/Notes/Patterns/orchard-survey-operating-method.md"
UNRELATED = "Knowledge Base/Notes/Research/orchard-survey-canopy-counts.md"
TURN = "Ilse Vandermeer asked whether the harbour gauges look healthy this week."
FILLERS = 45


def _write(vault: Path, rel: str, text: str) -> None:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _note(
    title: str,
    unit: str,
    *,
    block: str,
    link: bool = False,
    project: str | None = None,
    standing: bool = False,
    updated: str = "2026-09-20",
) -> str:
    front = f"type: note\nstatus: active\nupdated: {updated}\n"
    if project:
        front += f"project: {project}\n"
    if standing:
        front += "standing: true\n"
    links = f"\nRelated: [[{ENTITY.removesuffix('.md')}]]\n" if link else "\n"
    return f"---\n{front}---\n\n# {title}\n\n## Summary\n\n{unit} ^{block}\n{links}"


def _reindex(vault: Path) -> None:
    lexstore.ensure_fresh(vault)
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(vault).rebuild()


@pytest.fixture
def study_vault(vault: Path) -> Path:
    _write(
        vault,
        "Knowledge Base/_Schema/project-keys.yaml",
        "projects:\n  harbor-study:\n    folder: Harbor Study\n    category: research\n"
        "  orchard-survey:\n    folder: Orchard Survey\n    category: research\n",
    )
    _write(
        vault,
        ENTITY,
        "---\ntype: entity\nentity_type: person\nstatus: active\nproject: harbor-study\n---\n\n"
        "# Ilse Vandermeer\n\n## Summary\n\nA hydrographer who reviews the harbour gauges.\n",
    )
    for index in range(FILLERS):
        _write(
            vault,
            f"Knowledge Base/Notes/Research/aa-gauge-log-{index:02d}.md",
            _note(
                f"Gauge log {index:02d}",
                f"- [fact] Gauge reading batch {index:02d} was filed.",
                block=f"fill-{index}",
                link=True,
            ),
        )
    _write(
        vault,
        CONCLUSION,
        _note(
            "Tide panel priority subset",
            "- [decision] For the tide panel only gauges alpha and gamma are reviewed first.",
            block="conclusion",
            link=True,
        ),
    )
    _write(
        vault,
        STANDING,
        _note(
            "Harbor study operating method",
            "- [decision] Every claim about a gauge is argued from the raw log before any "
            "summary, and a summary never overrides the log.",
            block="method",
            project="harbor-study",
            standing=True,
            updated="2025-01-01",
        ),
    )
    _write(
        vault,
        OTHER_STANDING,
        _note(
            "Harbor study older method",
            "- [decision] An earlier method note that a newer one supersedes in practice.",
            block="older",
            project="harbor-study",
            standing=True,
            updated="2024-01-01",
        ),
    )
    _write(
        vault,
        FOREIGN_STANDING,
        _note(
            "Orchard survey operating method",
            "- [decision] Canopy counts are argued from the plot sheet before any estimate.",
            block="foreign",
            project="orchard-survey",
            standing=True,
        ),
    )
    _write(
        vault,
        UNRELATED,
        _note(
            "Orchard survey canopy counts",
            "- [decision] Canopy counts were closed for the season.",
            block="canopy",
            project="orchard-survey",
        ),
    )
    _reindex(vault)
    return vault


def _paths(packet: dict) -> set[str]:
    return {u["provenance"]["path"] for u in packet["units"]}


def _resolved(packet: dict) -> list[str]:
    return [a["path"] for a in packet["anchors"] if a["status"] == "resolved"]


# -- (a) the entity's own conclusion pages ---------------------------------- #


def test_an_entitys_conclusion_page_is_served_past_the_link_cap(study_vault: Path) -> None:
    packet = working_set.compile_packet(study_vault, turn=TURN, max_chars=6000)

    assert _resolved(packet) == [ENTITY]
    assert CONCLUSION in _paths(packet)
    unit = next(u for u in packet["units"] if u["provenance"]["path"] == CONCLUSION)
    assert unit["role"] == "precedents"


def test_an_unlinked_conclusion_and_a_linked_non_conclusion_stay_out(study_vault: Path) -> None:
    packet = working_set.compile_packet(study_vault, turn=TURN, max_chars=6000)

    assert UNRELATED not in _paths(packet)
    assert not any(path.rsplit("/", 1)[-1].startswith("aa-gauge-log") for path in _paths(packet))


def test_the_extra_conclusion_pages_are_bounded(study_vault: Path) -> None:
    for index in range(working_set.ENTITY_CONCLUSION_PAGES + 4):
        _write(
            study_vault,
            f"Knowledge Base/Notes/Decisions/zz-more-{index:02d}.md",
            _note(
                f"More {index:02d}",
                f"- [decision] Another settled point number {index:02d}.",
                block=f"more-{index}",
                link=True,
            ),
        )
    _reindex(study_vault)

    packet = working_set.compile_packet(study_vault, turn=TURN, max_chars=8000)

    served = [p for p in _paths(packet) if "/Decisions/zz-" in p]
    assert len(served) <= working_set.ENTITY_CONCLUSION_PAGES + working_set.MAX_ITEMS_PER_ROLE


# -- (b) the project's standing methodology page ---------------------------- #


def test_the_projects_standing_page_is_served_under_precedents(study_vault: Path) -> None:
    packet = working_set.compile_packet(study_vault, turn=TURN, max_chars=6000)

    assert STANDING in _paths(packet)
    unit = next(u for u in packet["units"] if u["provenance"]["path"] == STANDING)
    assert unit["role"] == "precedents"
    assert "raw log" in unit["text"]


def test_one_standing_page_per_project(study_vault: Path) -> None:
    packet = working_set.compile_packet(study_vault, turn=TURN, max_chars=6000)

    assert OTHER_STANDING not in _paths(packet)


def test_another_projects_standing_page_is_not_served(study_vault: Path) -> None:
    packet = working_set.compile_packet(study_vault, turn=TURN, max_chars=6000)

    assert FOREIGN_STANDING not in _paths(packet)


def test_a_turn_resolving_nothing_in_the_project_serves_no_standing_page(
    study_vault: Path,
) -> None:
    packet = working_set.compile_packet(
        study_vault, turn="What is the weather like on the coast today?", max_chars=6000
    )

    assert STANDING not in _paths(packet)


def test_the_standing_page_is_charged_to_the_packet_budget(study_vault: Path) -> None:
    packet = working_set.compile_packet(study_vault, turn=TURN, max_chars=working_set.MIN_BUDGET_CHARS)

    assert packet["budget"]["used_chars"] <= working_set.MIN_BUDGET_CHARS


def test_a_page_that_only_says_standing_in_another_project_is_not_adopted(
    study_vault: Path,
) -> None:
    """The declaration is per project: a page that declares itself standing for
    one project never stands for another the entity is not in."""
    packet = working_set.compile_packet(study_vault, turn=TURN, max_chars=6000)

    assert not ({FOREIGN_STANDING, UNRELATED} & _paths(packet))


# -- (c) a scoped claim keeps its qualifier --------------------------------- #

SCOPED = (
    "- [decision] The follow-up panel covers the harbour gauges and the tide "
    "tables that the survey team has reviewed since the spring campaign, and "
    "the reviewers agreed the ordering of the remaining sites by exposure, "
    "with the exposed sites first and the sheltered ones after the storm "
    "season, which is a standing arrangement and not a one-off, "
    "chronic exposure sites only."
)


def _item(text: str) -> working_set.LaneItem:
    return working_set.LaneItem(
        role="precedents",
        level="unit",
        ref="Knowledge Base/Notes/Decisions/scoped.md#u",
        path="Knowledge Base/Notes/Decisions/scoped.md",
        title="Scoped",
        text=text,
        lifecycle="active",
        updated="2026-09-20",
        anchor="Knowledge Base/Entities/People/Ilse Vandermeer.md",
        provenance={"category": "decision"},
        why="precedents lane",
    )


def _packet(text: str) -> dict:
    return working_set.build_packet(
        items=(_item(text),),
        anchors=(),
        roles=(),
        current_state=(),
        ambiguity=(),
        missing=(),
        max_chars=4000,
        generation={},
        status="resolved",
    )


def test_a_scoped_claim_longer_than_the_preferred_size_keeps_its_qualifier() -> None:
    assert len(SCOPED) > working_set.MAX_UNIT_CHARS
    packet = _packet(SCOPED)

    assert [u["text"] for u in packet["units"]] == [SCOPED.strip()]
    assert packet["units"][0]["text"].endswith("chronic exposure sites only.")


def test_a_unit_too_long_to_serve_whole_becomes_a_pointer_never_a_half_claim() -> None:
    packet = _packet(SCOPED + " " + "More detail about the sites follows. " * 40)

    assert packet["units"] == []
    assert [p["reason"] for p in packet["pointers"]] == ["unit_too_long"]


# -- a referent that only recency supplied is not read for conclusions ------- #


def _recency_referent(vault: Path, turn: str) -> dict:
    """The entity becomes the fresh session's referent by recency alone."""
    from exomem import commands, working_set_heat

    working_set_heat.reset_for_tests()
    working_set_runtime.reset_caches_for_tests()
    commands.op_activate_context(
        vault, turn="Let's pick this up.", anchor=ENTITY, session="earlier", workspace="bench"
    )
    working_set_runtime.reset_caches_for_tests()
    return commands.op_activate_context(vault, turn=turn, session="fresh", workspace="bench")


def test_a_recency_only_entity_gets_neither_conclusions_nor_the_standing_page(
    study_vault: Path,
) -> None:
    """The referent recency supplied is read through identity only. (That the
    cue 'before' selects `precedents` by another route is pinned below, where
    the reach is handed a `turn_cue` selection directly: a turn with that much
    content does not resolve by recency.)"""
    packet = _recency_referent(study_vault, "where were we")

    anchors = [a for a in packet["anchors"] if a["status"] == "resolved"]
    assert anchors and set(anchors[0]["evidence"]) == {"recency"}
    assert not ({CONCLUSION, STANDING} & _paths(packet))


def test_a_recency_only_project_gets_no_standing_page_on_a_bare_referential_turn(
    study_vault: Path,
) -> None:
    from exomem import commands, working_set_heat

    working_set_heat.reset_for_tests()
    working_set_runtime.reset_caches_for_tests()
    commands.op_activate_context(
        study_vault,
        turn="Let's pick this up.",
        anchor="project:harbor-study",
        session="earlier",
        workspace="bench",
    )
    working_set_runtime.reset_caches_for_tests()
    packet = commands.op_activate_context(
        study_vault, turn="where were we", session="fresh", workspace="bench"
    )

    assert STANDING not in _paths(packet)


def test_reach_precedents_skips_an_anchor_reached_by_recency_alone(study_vault: Path) -> None:
    from exomem import context_roles

    index = working_set_index.WorkingSetIndex(study_vault)
    row = next(r for r in index.anchors() if r.path == ENTITY)

    def anchor(evidence: tuple[str, ...]):
        return working_set_resolve.ResolvedAnchor(
            anchor_id=row.anchor_id,
            path=row.path,
            ref=None,
            title=row.title,
            kind="entity",
            lifecycle="active",
            status="resolved",
            evidence=evidence,
            categories=(),
            neighbourhood=frozenset(),
        )

    roles = [{"id": "precedents", "source": "turn_cue", "lane": "units"}]
    registry = context_roles.load_roles(study_vault)

    def reach(evidence):
        return working_set.reach_precedents(
            study_vault, resolved=[anchor(evidence)], roles=roles, registry=registry, index=index
        )

    named = reach(("exact_alias",))
    prior = reach(("recency",))
    assert CONCLUSION in named[0] and STANDING in named[1]
    assert prior == (frozenset(), frozenset())
