"""Baseline of what relation words do today, recorded before any reader reads a behaviour.

The change `make-epistemic-language-dynamic` replaces the hard-coded relation
sets in the readers with registry reads (relation behaviours and the frontmatter
binding). This module is the before-picture on one fixture vault. Each reader
conversion must keep every "core" assertion green and flip exactly the
assertions tagged with its own intended-change number (1-12, as numbered in the
change's design.md). A flip is a one-line edit of the tagged value.

Tags: `# CORE` assertions must never change. `# FLIPS <n>` assertions recorded
the value before intended change `<n>` and hold the new value once the commit
that converts the reader has landed.
"""

from __future__ import annotations

import datetime as dt
import uuid
from pathlib import Path

import pytest
import test_governance_projected_runtime_lanes as projection_lanes
from test_working_set_carry import CARRY_PAGE, SLED_ANCHOR, _seed_carry_pages
from test_working_set_index import _seed_planning, _seed_structure

from exomem import (
    activation,
    artifact_role_state,
    audit,
    claims,
    commands,
    context_catalogue,
    contradiction_stance,
    dreamer_families,
    epistemic_graph,
    find,
    freshness,
    lexstore,
    referent_resolution,
    referent_runtime,
    relation_census,
    relation_registry,
    request_budget,
    semantic_contract,
    semantic_index,
    semantic_units,
    sensed_model,
    traversal_profiles,
    unit_corrections,
    working_set,
    working_set_currency,
    working_set_index,
    working_set_runtime,
)
from exomem import (
    vault as vault_module,
)
from exomem.governance import principal, projected_graph, projection_runtime, projections
from exomem.governance.principal import owner_principal, request_scope

KB = "Knowledge Base/Notes/Insights"
ENTITIES = "Knowledge Base/Entities/Organizations"
TODAY = dt.date(2026, 11, 8)

# The vault's saved relation overlay: one extension per core parent, one
# control, and one grandfathered extension whose saved family differs from
# its parent's.
OVERLAY = """\
schema_version: 1
extensions:
  x.backs: {parent: supports, description: Backs a claim}
  x.based_on: {parent: derived_from, description: Is based on}
  x.closes: {parent: resolves, description: Closes a question}
  x.retires: {parent: supersedes, description: Retires an earlier view}
  x.disputes: {parent: contradicts, description: Disputes a claim}
  x.see_also: {parent: links_to, description: Points to related reading}
  x.concerns: {parent: about_entity, description: Concerns an entity, aliases: [concerns]}
  x.qualifies: {parent: refines, description: Qualifies a claim}
  x.leans_on: {parent: relates_to, family: support, description: Leans on a claim}
  x.belongs_to: {parent: member_of, description: Belongs to a group}
  x.joins: {parent: relates_to, family: membership, description: Joins a group}
"""


# Relations a due prediction and an old question carry on the unit, to see which clear it.
AUDIT_RELATIONS = (
    "supports",
    "contradicts",
    "resolves",
    "evidenced_by",
    "answers",
    "refines",
    "x.backs",
    "x.closes",
    "x.disputes",
    "x.qualifies",
)


def _audit_name(relation: str) -> str:
    return "aud-" + relation.replace(".", "-").replace("_", "-")


SOURCES = "Knowledge Base/Sources"

# Pages that name one entity, by the relation each uses.
CONTRIBUTOR_RELATIONS = {
    "supports": "supports",
    "cites": "cites",
    "evidenced": "evidenced_by",
    "derived": "derived_from",
    "based": "x.based_on",
}

# Organizations that point at one group, by the relation each uses.
MEMBER_RELATIONS = {
    "core": "member_of",
    "extension": "x.belongs_to",
    "grandfathered": "x.joins",
    "link": "links_to",
}

# The seven frontmatter fields that name a relation, and the relation each states.
FIELDS = (
    "sources",
    "evidence",
    "evidences",
    "evidence_paths",
    "supersedes",
    "superseded_by",
    "related",
)

# Relation words a page-level row may use, to see which count as a typed edge.
QUALIFY_RELATIONS = (
    "supports",
    "refines",
    "answers",
    "contradicts",
    "supersedes",
    "relates_to",
    "links_to",
    "cites",
    "derived_from",
    "evidenced_by",
    "mentions",
    "observed_in",
    "x.backs",
    "x.qualifies",
    "x.see_also",
    "x.based_on",
    "x.retires",
    "x.leans_on",
    "frobnicates",
)


def _fm_target(field: str) -> str:
    return "fm-t-" + field.replace("_", "-")


def _qual_name(relation: str) -> str:
    return "qual-" + relation.replace(".", "-").replace("_", "-")


def _write(vault: Path, rel: str, text: str) -> Path:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _page(
    vault: Path,
    name: str,
    body: str = "",
    *,
    front: str = "",
    page_type: str = "insight",
    status: str = "active",
    created: str = "2026-10-01",
    folder: str = KB,
) -> str:
    """Write one page and return its vault-relative path without the extension."""
    rel = f"{folder}/{name}"
    _write(
        vault,
        f"{rel}.md",
        f"---\ntype: {page_type}\nstatus: {status}\ntitle: {name}\ncreated: {created}\n"
        f"updated: {created}\n{front}---\n# {name} note\n\n{body}\n",
    )
    return rel


def _unit(heading: str, anchor: str, relations: str = "", text: str = "A unit.") -> str:
    rows = f"- id: {anchor}\n" + (f"- relations: {relations}\n" if relations else "")
    return f"## {heading}\n{rows}\n{text}\n\n"


def _link(rel: str) -> str:
    return f"[[{rel}]]"


def build_fixture_vault(root: Path) -> Path:
    """The one fixture vault, written fresh for each test."""
    _write(root, "Knowledge Base/_Schema/relation-registry.yaml", OVERLAY)

    # Unit relations a page may lift to a page-level proposal. Three per page,
    # because one page proposes at most three lifts.
    target = _page(root, "lift-target", "The page every lift points at.")
    link = _link(target)
    _page(
        root,
        "lift-a",
        _unit("Claim", "a1", f"answers: {link}")
        + _unit("Claim", "a2", f"resolves: {link}")
        + _unit("Claim", "a3", f"raises_question: {link}"),
    )
    _page(
        root,
        "lift-b",
        _unit("Claim", "b1", f"supports: {link}")
        + _unit("Claim", "b2", f"contradicts: {link}")
        + _unit("Claim", "b3", f"refines: {link}"),
    )
    _page(
        root,
        "lift-c",
        _unit("Claim", "c1", f"evidenced_by: {link}")
        + _unit("Claim", "c2", f"duplicates: {link}")
        + _unit("Claim", "c3", f"causes: {link}"),
    )
    _page(
        root,
        "lift-d",
        _unit("Claim", "d1", f"x.qualifies: {link}")
        + _unit("Claim", "d2", f"x.leans_on: {link}")
        + _unit("Claim", "d3", f"x.retires: {link}"),
    )

    # Pages that answer or close one target, and a pair that both resolve one.
    answered = _link(_page(root, "res-target", "Something that gets answered."))
    _page(root, "res-a", _unit("Claim", "r1", f"answers: {answered}"))
    _page(root, "res-b", _unit("Claim", "r2", f"answers: {answered}"))
    _page(root, "res-c", _unit("Claim", "r3", f"x.closes: {answered}"))
    resolved = _link(_page(root, "rr-target", "Something that gets resolved twice."))
    _page(root, "rr-1", _unit("Claim", "s1", f"resolves: {resolved}"))
    _page(root, "rr-2", _unit("Claim", "s2", f"resolves: {resolved}"))

    # Two pages that carry the same open question.
    question = "Which store holds the sessions?"
    _page(root, "oq-a", _unit("Open Question", "q1", text=question))
    _page(root, "oq-b", _unit("Open Question", "q2", text=question))

    # Pages that name the same Source: by frontmatter field, and by a page-level
    # relation in the extension of `derived_from`.
    source = _page(root, "src-doc", "A document other pages derive from.")
    _page(root, "fs-a", front=f'sources: ["{_link(source)}"]\n')
    _page(root, "fs-b", front=f'sources: ["{_link(source)}"]\n')
    _page(root, "fs-c", f"## Relations\n- x.based_on {_link(source)}\n")

    # Organizations: the pages a seed page reaches through one relation each.
    def entity(name: str) -> str:
        return _page(
            root,
            name,
            front="entity_type: organization\n",
            page_type="entity",
            folder=ENTITIES,
        )

    neighbours = {
        label: entity(f"nb-{label}")
        for label in ("links", "supports", "see-also", "backs", "unregistered", "leans", "about")
    }
    _page(
        root,
        "nb-seed",
        "## Relations\n"
        f"- links_to {_link(neighbours['links'])}\n"
        f"- supports {_link(neighbours['supports'])}\n"
        f"- x.see_also {_link(neighbours['see-also'])}\n"
        f"- x.backs {_link(neighbours['backs'])}\n"
        f"- frobnicates {_link(neighbours['unregistered'])}\n"
        f"- x.leans_on {_link(neighbours['leans'])}\n"
        f"- about_entity {_link(neighbours['about'])}\n",
    )

    # The entities one page reaches by relation, for hydration.
    hydrated = {
        label: entity(f"hy-{label}")
        for label in ("supports", "cites", "evidenced", "derived", "based")
    }
    _page(
        root,
        "hy-page",
        "## Relations\n"
        f"- supports {_link(hydrated['supports'])}\n"
        f"- cites {_link(hydrated['cites'])}\n"
        f"- evidenced_by {_link(hydrated['evidenced'])}\n"
        f"- derived_from {_link(hydrated['derived'])}\n"
        f"- x.based_on {_link(hydrated['based'])}\n",
    )

    # Organizations that join one group, by the core word, by its extension and by
    # a grandfathered extension whose saved family is `membership`.
    group = _link(entity("mem-group"))
    for label, relation in MEMBER_RELATIONS.items():
        _page(
            root,
            f"mem-{label}",
            f"## Relations\n- {relation} {group}\n",
            front="entity_type: organization\n",
            page_type="entity",
            folder=ENTITIES,
        )

    # An entity that newer pages name through one relation each, every page citing
    # its own Source so the pages count as independent contributors.
    hub = _link(
        _page(
            root,
            "hub",
            front="entity_type: organization\n",
            page_type="entity",
            folder=ENTITIES,
            created="2026-09-01",
        )
    )
    for label, relation in CONTRIBUTOR_RELATIONS.items():
        document = _page(root, f"ct-doc-{label}", "A document.", page_type="source", folder=SOURCES)
        _page(
            root,
            f"ct-{label}",
            _unit("Claim", "c1", text=f"A fact about the hub, from the {label} page.")
            + f"## Relations\n- {relation} {hub}\n",
            front=f'sources: ["{_link(document)}"]\n',
            created="2026-10-01",
        )

    # Pairs that declare a contradiction, by the core word and by its extension.
    _page(root, "con-a", f"## Relations\n- contradicts {_link(f'{KB}/con-b')}\n")
    _page(root, "con-b", "The other side.")
    _page(root, "dis-a", f"## Relations\n- x.disputes {_link(f'{KB}/dis-b')}\n")
    _page(root, "dis-b", "The other side of a dispute.")

    # One page per relation, each with a due prediction and an old open question
    # that carry that relation on the unit itself.
    audit_target = _link(_page(root, "audit-target", "What the units point at."))
    for relation in AUDIT_RELATIONS:
        _page(
            root,
            _audit_name(relation),
            f"## Prediction\n- id: p1\n- check_by: 2026-10-01\n- relations: {relation}: "
            f"{audit_target}\n\nThe backlog clears within a week.\n\n"
            f"## Open Question\n- id: q1\n- relations: {relation}: {audit_target}\n\n"
            f"Which store holds the {relation} records?\n",
        )

    # One page that uses all seven frontmatter fields, each naming its own page.
    fm_targets = {field: _page(root, _fm_target(field), "Named by a field.") for field in FIELDS}
    _page(
        root,
        "fm-all",
        "A page that uses every frontmatter field that names a relation.",
        front="".join(f'{field}: ["{_link(fm_targets[field])}"]\n' for field in FIELDS),
    )

    # Pages that supersede others: a pair that point at each other, a pointer that
    # is not returned, and one page that supersedes through a core word and an
    # extension.
    _page(root, "sup-new", front=f'supersedes: ["{_link(f"{KB}/sup-old")}"]\n')
    _page(
        root,
        "sup-old",
        status="superseded",
        front=f'superseded_by: ["{_link(f"{KB}/sup-new")}"]\n',
    )
    _page(root, "sup-one-new", front=f'supersedes: ["{_link(f"{KB}/sup-one-old")}"]\n')
    _page(root, "sup-one-old", status="superseded")
    rv_old_a = _link(_page(root, "rv-old-a", "Replaced through the core word."))
    rv_old_b = _link(_page(root, "rv-old-b", "Replaced through the extension."))
    _page(
        root,
        "rv-new",
        f"## Relations\n- supersedes {rv_old_a}\n- x.retires {rv_old_b}\n",
    )

    # A page whose units replace other units on it, through each kind of word.
    _page(
        root,
        "rs-page",
        _unit("Claim", "u1", text="Old wording one.")
        + _unit("Claim", "u2", text="Old wording two.")
        + _unit("Claim", "u3", text="Old wording three.")
        + _unit("Claim", "n1", "supersedes: [[#u1]]", "New wording one.")
        + _unit("Claim", "n2", "x.retires: [[#u2]]", "New wording two.")
        + _unit("Claim", "n3", "supports: [[#u3]]", "New wording three."),
    )

    # One page per relation a page-level row may use, each pointing at one insight.
    qualify_target = _link(_page(root, "qual-target", "What the relation rows point at."))
    for relation in QUALIFY_RELATIONS:
        _page(root, _qual_name(relation), f"## Relations\n- {relation} {qualify_target}\n")
    return root


def build_graph(root: Path) -> epistemic_graph.EpistemicGraphIndex:
    index = epistemic_graph.EpistemicGraphIndex(root)
    index.rebuild_all()
    return index


@pytest.fixture
def fixture_vault(tmp_path: Path) -> Path:
    return build_fixture_vault(tmp_path / "vault")


def _short(path: str) -> str:
    return path.removeprefix(f"{KB}/").removeprefix(f"{ENTITIES}/").removesuffix(".md")


def _suggested(vault: Path, name: str, method: str) -> list[tuple[str, str]]:
    """The (relation, target) pairs one suggestion method proposes for one page."""
    found = epistemic_graph.suggest_relations(vault, path=f"{KB}/{name}.md", limit=50)
    return sorted(
        (c["relation_type"], _short(c["to"])) for c in found["candidates"] if c["method"] == method
    )


# ---------------------------------------------------------------------------
# Relation suggestions: lifts, shared targets, shared questions, shared sources
# ---------------------------------------------------------------------------


def test_a_unit_relation_lifts_when_its_family_is_a_lift_family(fixture_vault: Path) -> None:
    """Eight core relations lift to a page proposal; `causes` and its control stay unlifted."""
    build_graph(fixture_vault)

    lifted = {
        name: [
            relation for relation, _target in _suggested(fixture_vault, name, "unit_relation_lift")
        ]
        for name in ("lift-a", "lift-b", "lift-c")
    }

    assert lifted == {  # CORE
        "lift-a": ["answers", "raises_question", "resolves"],
        "lift-b": ["contradicts", "refines", "supports"],
        "lift-c": ["duplicates", "evidenced_by"],
    }
    # An extension whose family equals its parent's lifts like the parent.
    assert "x.qualifies" in [  # CORE
        relation for relation, _t in _suggested(fixture_vault, "lift-d", "unit_relation_lift")
    ]


def test_a_grandfathered_extension_lifts_by_its_saved_family(fixture_vault: Path) -> None:
    """FLIPS 9: `x.leans_on` has parent `relates_to` and saved family `support`.

    The saved family used to decide, so it lifted. It now follows its parent
    `relates_to`, which has no `lifts`, so it does not.
    """
    build_graph(fixture_vault)

    relations = [r for r, _t in _suggested(fixture_vault, "lift-d", "unit_relation_lift")]

    assert "x.leans_on" not in relations  # FLIPS 9
    assert "x.retires" not in relations  # CORE: a supersession is never lifted


def test_two_pages_that_answer_one_target_are_proposed_as_related(fixture_vault: Path) -> None:
    build_graph(fixture_vault)

    # `res-c` states `x.closes`; FLIPS 3 owns it, so the core pair is read without it.
    assert [
        item
        for item in _suggested(fixture_vault, "res-a", "shared_resolution_target")
        if item != ("relates_to", "res-c")
    ] == [("relates_to", "res-b")]  # CORE
    assert _suggested(fixture_vault, "rr-1", "shared_resolution_target") == [  # CORE
        ("relates_to", "rr-2")
    ]


def test_an_extension_of_resolves_does_not_feed_shared_resolution_candidates(
    fixture_vault: Path,
) -> None:
    """FLIPS 3: `x.closes` (parent `resolves`) was not in the key set.

    It now feeds the candidates, so `res-c` is proposed beside `res-a` and `res-b`.
    """
    build_graph(fixture_vault)

    assert _suggested(fixture_vault, "res-c", "shared_resolution_target") == [  # FLIPS 3
        ("relates_to", "res-a"),
        ("relates_to", "res-b"),
    ]
    assert ("relates_to", "res-c") in _suggested(  # FLIPS 3
        fixture_vault, "res-a", "shared_resolution_target"
    )


def test_two_pages_that_carry_one_question_are_proposed_as_related(fixture_vault: Path) -> None:
    build_graph(fixture_vault)

    assert _suggested(fixture_vault, "oq-a", "shared_open_question") == [  # CORE
        ("relates_to", "oq-b")
    ]


def test_frontmatter_sources_and_shared_sources_are_proposed(fixture_vault: Path) -> None:
    build_graph(fixture_vault)

    assert _suggested(fixture_vault, "fs-a", "frontmatter_sources") == [  # CORE
        ("derived_from", "src-doc")
    ]
    # `fs-c` states `x.based_on`; FLIPS 8 owns it, so the core pair is read without it.
    assert [
        item
        for item in _suggested(fixture_vault, "fs-a", "shared_sources")
        if item != ("relates_to", "fs-c")
    ] == [("relates_to", "fs-b")]  # CORE


def test_a_page_relation_in_an_extension_of_derived_from_seeds_no_shared_source(
    fixture_vault: Path,
) -> None:
    """FLIPS 8: `fs-c` states `x.based_on` the same Source as `fs-a` and `fs-b`.

    The shared-source query used to read the key `derived_from` only, so `fs-c`
    seeded nothing. It is now proposed beside them.
    """
    build_graph(fixture_vault)

    assert _suggested(fixture_vault, "fs-c", "shared_sources") == [  # FLIPS 8
        ("relates_to", "fs-a"),
        ("relates_to", "fs-b"),
    ]
    assert ("relates_to", "fs-c") in _suggested(  # FLIPS 8
        fixture_vault, "fs-a", "shared_sources"
    )


def test_the_review_batch_carries_the_lift_and_source_rows(fixture_vault: Path) -> None:
    index = build_graph(fixture_vault)

    batch = index.relation_review_batch()
    rows = {
        _short(group["path"]): sorted(
            (item["method"], item["relation_type"], _short(item["to"]))
            for item in group["items"]
            if item["method"] != "wikilink"
        )
        for group in batch["groups"]
    }

    assert rows["lift-a"] == [  # CORE
        ("unit_relation_lift", "answers", "lift-target"),
        ("unit_relation_lift", "raises_question", "lift-target"),
        ("unit_relation_lift", "resolves", "lift-target"),
    ]
    assert rows["fs-a"] == [  # CORE
        ("frontmatter_sources", "derived_from", "src-doc"),
        ("shared_sources", "relates_to", "fs-b"),
    ]
    assert rows["res-c"] == [  # FLIPS 3
        ("shared_resolution_target", "relates_to", "res-a"),
        ("shared_resolution_target", "relates_to", "res-b"),
        ("unit_relation_lift", "x.closes", "res-target"),
    ]
    assert rows["lift-d"] == [  # FLIPS 9
        ("unit_relation_lift", "x.qualifies", "lift-target"),
    ]


# ---------------------------------------------------------------------------
# Declared pairs: which pages the author already set against each other
# ---------------------------------------------------------------------------


def _pair(index: epistemic_graph.EpistemicGraphIndex, a: str, b: str) -> str | None:
    return contradiction_stance.structural_pair(
        index.vault_root, f"{KB}/{a}.md", f"{KB}/{b}.md", index=index
    )


def test_pages_that_contradict_or_answer_one_target_are_a_declared_pair(
    fixture_vault: Path,
) -> None:
    index = build_graph(fixture_vault)

    assert _pair(index, "con-a", "con-b") == "contradicts"  # CORE
    # An extension of `contradicts` is already read as a contradiction.
    assert _pair(index, "dis-a", "dis-b") == "contradicts"  # CORE
    assert _pair(index, "res-a", "res-b") == "answers_same_question"  # CORE


def test_pages_that_resolve_one_target_are_not_a_declared_pair(fixture_vault: Path) -> None:
    """FLIPS 10: stance reads `answers` only, so two `resolves` pages are no pair.

    After intended change 10, stance reads `settles` and both assertions below
    return `"answers_same_question"`. The `res-a`/`res-c` line also reflects
    the extension reaching through its parent (`x.closes` is an extension of
    `resolves`), which follows from the same read.
    """
    index = build_graph(fixture_vault)

    assert _pair(index, "rr-1", "rr-2") is None  # FLIPS 10
    assert _pair(index, "res-a", "res-c") is None  # FLIPS 10


# ---------------------------------------------------------------------------
# Entity hydration: which linked pages count as content links
# ---------------------------------------------------------------------------


def _hydrated(vault: Path, name: str) -> list[str]:
    context = dreamer_families.Context(vault_root=vault, store=None, conn=None, now=0.0)
    try:
        return sorted(
            _short(path)
            for path in dreamer_families._hydration_entities(context, f"{KB}/{name}.md")
        )
    finally:
        context.close()


def test_hydration_counts_a_supported_entity_and_skips_a_derived_one(
    fixture_vault: Path,
) -> None:
    build_graph(fixture_vault)

    hydrated = _hydrated(fixture_vault, "hy-page")

    assert "hy-supports" in hydrated  # CORE
    assert "hy-derived" not in hydrated  # CORE: `derived_from` is provenance, not a link


def test_hydration_counts_a_citation_and_an_evidence_edge_as_content_links(
    fixture_vault: Path,
) -> None:
    """FLIPS 11: only `derived_from` is skipped today.

    After intended change 11 every `sources` relation is skipped, so `cites`
    and `evidenced_by` stop counting.
    """
    build_graph(fixture_vault)

    hydrated = _hydrated(fixture_vault, "hy-page")

    assert "hy-cites" in hydrated  # FLIPS 11
    assert "hy-evidenced" in hydrated  # FLIPS 11


def test_hydration_counts_an_extension_of_derived_from_as_a_content_link(
    fixture_vault: Path,
) -> None:
    """FLIPS 2: the skip reads the key `derived_from`, so `x.based_on` counts today.

    After intended change 2 the extension is provenance through its parent and
    is skipped.
    """
    build_graph(fixture_vault)

    assert "hy-based" in _hydrated(fixture_vault, "hy-page")  # FLIPS 2


# ---------------------------------------------------------------------------
# Plain links and typed edges: neighbours, referent ranking, projected graph
# ---------------------------------------------------------------------------

# What each relation on `nb-seed` reaches, as (relation, typed).
NEIGHBOURS = {
    "nb-links": ("links_to", False),
    "nb-supports": ("supports", True),
    "nb-see-also": ("x.see_also", False),
    "nb-backs": ("x.backs", True),
    "nb-unregistered": (None, False),
    "nb-leans": ("x.leans_on", True),
    "nb-about": ("about_entity", True),
}


def test_a_neighbour_is_typed_unless_its_edge_is_a_plain_or_unregistered_link(
    fixture_vault: Path,
) -> None:
    """An unregistered relation is not typed: it ranks as a plain link."""
    index = build_graph(fixture_vault)

    neighbours = {
        _short(n.other_rel): (n.relation_type, n.typed)
        for n in index.neighbors_for([f"{KB}/nb-seed.md"])
    }

    assert neighbours == NEIGHBOURS  # CORE


def test_referent_resolution_ranks_a_plain_or_unregistered_edge_last(fixture_vault: Path) -> None:
    """Tier 1 is a plain link; an edge with no registered relation reads as `links_to`."""
    index = build_graph(fixture_vault)
    seed = f"{KB}/nb-seed.md"
    entities = tuple(
        referent_resolution.EntityRecord(
            path=f"{ENTITIES}/{name}.md",
            title=name,
            entity_type="organization",
            status="active",
            aliases=(),
            tags=(),
            relationship="",
            affiliation="",
        )
        for name in NEIGHBOURS
    )
    edges = referent_runtime._edge_facts(
        index.vault_root,
        anchors=[seed],
        entity_paths=frozenset(entity.path for entity in entities),
        graph=True,
        anchor_cap=10,
    )
    cue = referent_resolution.detect_cue("which company")
    assert cue is not None

    resolution = referent_resolution.resolve_referents(
        cue=cue,
        hits=(
            referent_resolution.HitFact(
                path=seed,
                type="insight",
                title="seed",
                status="active",
                rank=1,
                bm25_rank=1,
                vector_rank=None,
                keyword_rank=1,
            ),
        ),
        entities=entities,
        edges=edges,
    )

    tiers = {
        _short(match.path): [
            (item.detail["relation_type"], item.detail["tier"])
            for item in match.evidence
            if item.kind == "graph"
        ][0]
        for match in (*resolution.resolved, *resolution.candidates)
    }
    assert tiers == {  # CORE
        "nb-links": ("links_to", 1),
        "nb-supports": ("supports", 0),
        "nb-see-also": ("x.see_also", 1),
        "nb-backs": ("x.backs", 0),
        "nb-unregistered": ("links_to", 1),
        "nb-leans": ("x.leans_on", 0),
        "nb-about": ("about_entity", 0),
    }


def test_projected_graph_lane_ranks_a_plain_link_below_a_typed_edge(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The projected lane tiers by relation key; stored edges carry no extension parent.

    `links_to` is the only plain link. An unregistered relation key and an
    extension of `links_to` both rank with the typed edges today.
    """
    for name in ("EXOMEM_DISABLE_EMBEDDINGS", "EXOMEM_DISABLE_CLIP", "EXOMEM_DISABLE_RANKING"):
        monkeypatch.delenv(name, raising=False)
    edges = {
        "a-links": "links_to",
        "b-supports": "supports",
        "c-see-also": "x.see_also",
        "d-unregistered": "frobnicates",
    }
    seed = projection_lanes._variant("Knowledge Base/seed.md", "e" * 64, "seed match")
    neighbours = {
        name: projection_lanes._variant(
            f"Knowledge Base/{name}.md", f"{index}" * 64, f"{name} text"
        )
        for index, name in enumerate(edges)
    }
    graph = projected_graph.ProjectionGraphMeasurement(
        projections.MeasurementKey(
            projection_variant_id=seed.projection_variant_id,
            lane="graph",
            extractor_version="projected-graph-v1",
            model_version="graph-schema-v1",
        ),
        tuple(
            projected_graph.ProjectionGraphEdge(
                seed.item_identity, neighbours[name].item_identity, relation
            )
            for name, relation in edges.items()
        ),
    )
    quiet = tuple(projection_lanes._graph(variant) for variant in neighbours.values())
    runtime = projection_lanes._runtime(
        tuple(projection_lanes._item(v) for v in (seed, *neighbours.values())),
        graphs=(graph, *quiet),
    )

    result = projection_runtime.find_projected_hits(
        tmp_path,
        runtime,
        query="seed",
        limit=5,
        mode="hybrid",
        graph=True,
        rerank=False,
        principal=principal.owner_principal(surface="library"),
        purpose=None,
    )

    order = [Path(hit.path).stem for hit in result.hits]
    typed = order[1:-1]
    assert order[0] == "seed"
    assert order[-1] == "a-links"  # CORE: the plain link ranks last
    assert "b-supports" in typed  # CORE
    # CORE: unlike the referent tier, this lane keeps an unregistered key typed.
    assert "d-unregistered" in typed
    assert "c-see-also" in typed  # FLIPS 6


# ---------------------------------------------------------------------------
# Audit: a due prediction or an old question stays open until a relation settles it
# ---------------------------------------------------------------------------


def _still_open(vault: Path, category: str) -> list[str]:
    """The relations whose page still has the finding, so did not clear it."""
    report = audit.audit(vault, categories=[category], today=TODAY)
    paths = {finding.path for finding in report.findings if finding.category == category}
    return [relation for relation in AUDIT_RELATIONS if f"{KB}/{_audit_name(relation)}.md" in paths]


def test_a_core_relation_that_bears_on_a_prediction_clears_it(fixture_vault: Path) -> None:
    still_open = _still_open(fixture_vault, "prediction_window")

    for relation in ("supports", "contradicts", "resolves", "evidenced_by"):
        assert relation not in still_open  # CORE
    for relation in ("answers", "refines", "x.qualifies"):
        assert relation in still_open  # CORE


def test_an_extension_of_a_clearing_relation_clears_a_prediction(
    fixture_vault: Path,
) -> None:
    """FLIPS 1: the set used to hold core keys, so an extension of `supports` was not in it.

    Each of these now clears the prediction through its parent.
    """
    still_open = _still_open(fixture_vault, "prediction_window")

    for relation in ("x.backs", "x.closes", "x.disputes"):
        assert relation not in still_open  # FLIPS 1


def test_a_core_relation_that_bears_on_a_question_clears_it(fixture_vault: Path) -> None:
    still_open = _still_open(fixture_vault, "question_aging")

    for relation in ("supports", "contradicts", "resolves", "evidenced_by"):
        assert relation not in still_open  # CORE
    for relation in ("answers", "refines", "x.qualifies"):
        assert relation in still_open  # CORE


def test_an_extension_of_a_clearing_relation_answers_a_question(
    fixture_vault: Path,
) -> None:
    """FLIPS 1: the question check shares the prediction check's rule."""
    still_open = _still_open(fixture_vault, "question_aging")

    for relation in ("x.backs", "x.closes", "x.disputes"):
        assert relation not in still_open  # FLIPS 1


# ---------------------------------------------------------------------------
# Frontmatter fields: the graph, the relation facts and activation record one edge
# ---------------------------------------------------------------------------

# For each field: the relation it states, and the (source, target) pages of the edge.
# A `superseded_by` field points at the page that replaces this one, so the edge
# runs from that page to this one.
FIELD_EDGES = {
    "sources": ("derived_from", "fm-all", "fm-t-sources"),
    "evidence": ("evidenced_by", "fm-all", "fm-t-evidence"),
    "evidences": ("evidenced_by", "fm-all", "fm-t-evidences"),
    "evidence_paths": ("evidenced_by", "fm-all", "fm-t-evidence-paths"),
    "supersedes": ("supersedes", "fm-all", "fm-t-supersedes"),
    "superseded_by": ("supersedes", "fm-t-superseded-by", "fm-all"),
    "related": ("links_to", "fm-all", "fm-t-related"),
}


def test_the_graph_records_one_edge_per_frontmatter_field(fixture_vault: Path) -> None:
    index = build_graph(fixture_vault)

    recorded = {
        edge["source_anchor"]: (
            edge["relation_type"],
            _short(edge["src_key"].removeprefix("file:")),
            _short(edge["dst_key"].removeprefix("file:")),
        )
        for edge in index.edges(source_path=f"{KB}/fm-all.md")
        if edge["origin"] == "frontmatter"
    }

    assert recorded == FIELD_EDGES  # CORE


def test_the_relation_facts_record_the_same_edge_per_frontmatter_field(
    fixture_vault: Path,
) -> None:
    corpus = semantic_contract.build_corpus_context(fixture_vault)

    recorded = {
        fact.authored_anchor: (
            fact.canonical_relation,
            _short(fact.logical_source_path),
            _short(fact.logical_target_path),
        )
        for fact in corpus.relation_facts
        if fact.origin == "frontmatter" and fact.authored_path == f"{KB}/fm-all.md"
    }

    assert recorded == FIELD_EDGES  # CORE


# What activation counts for a page that names one page through one field:
# (typed relations, provenance relations, frontmatter links).
FIELD_ACTIVATION = {
    "sources": (1, 1, 1),
    "evidence": (1, 1, 1),
    "evidences": (1, 1, 1),
    "evidence_paths": (1, 1, 1),
    "supersedes": (1, 0, 1),
    "superseded_by": (1, 0, 1),
    "related": (0, 0, 1),
}


def _activation(vault: Path, *, body: str = "", front: dict | None = None) -> dict:
    registry = relation_registry.load_registry(vault)
    document = semantic_units.parse_semantic_units(
        body,
        validate=False,
        relation_registry=registry,
        include_legacy_relations=True,
        retain_unknown_relations=True,
    )
    return activation.measure_document(
        document,
        registry,
        project=None,
        page_type=None,
        body_wikilinks=0,
        frontmatter_counts=activation.frontmatter_link_counts(front or {}),
    )


@pytest.mark.parametrize("field", FIELDS)
def test_activation_counts_each_frontmatter_field_by_the_relation_it_states(
    fixture_vault: Path, field: str
) -> None:
    """Each field adds to the typed count, the provenance count, or neither.

    Activation records no edge direction, so it asserts counts per field.
    """
    measured = _activation(fixture_vault, front={field: [_link(_fm_target(field))]})

    assert (  # CORE
        measured["typed_relations"],
        measured["provenance_relations"],
        measured["frontmatter_links"],
    ) == FIELD_ACTIVATION[field]


# For a page-level row in one relation: (typed relations, provenance relations, unregistered).
ROW_ACTIVATION = {
    "cites": (1, 1, 0),  # CORE
    "derived_from": (1, 1, 0),  # CORE
    "x.backs": (1, 0, 0),  # CORE
    "frobnicates": (0, 0, 1),  # CORE: an unregistered word is no typed edge
}


@pytest.mark.parametrize("relation", ROW_ACTIVATION)
def test_activation_counts_a_page_row_by_its_relation(fixture_vault: Path, relation: str) -> None:
    measured = _activation(
        fixture_vault, body=f"## Relations\n- {relation} {_link(f'{KB}/qual-target')}\n"
    )

    assert (
        measured["typed_relations"],
        measured["provenance_relations"],
        len(measured["unregistered"]),
    ) == ROW_ACTIVATION[relation]


def test_activation_counts_a_row_in_an_extension_of_derived_from_as_provenance(
    fixture_vault: Path,
) -> None:
    """FLIPS 2: the provenance set held core keys, so `x.based_on` counted as typed only.

    It now counts as provenance through its parent.
    """
    measured = _activation(
        fixture_vault, body=f"## Relations\n- x.based_on {_link(f'{KB}/qual-target')}\n"
    )

    assert measured["typed_relations"] == 1  # CORE
    assert measured["provenance_relations"] == 1  # FLIPS 2


# ---------------------------------------------------------------------------
# Relation review: which relation facts are a deliberate typed edge
# ---------------------------------------------------------------------------


def _qualification(vault: Path, relation: str) -> tuple[str, ...]:
    """The reasons one page-level row is not a typed edge; empty when it qualifies."""
    registry = relation_registry.load_registry(vault)
    corpus = semantic_contract.build_corpus_context(vault, registry=registry)
    (fact,) = [
        fact
        for fact in corpus.relation_facts
        if fact.authored_path == f"{KB}/{_qual_name(relation)}.md" and fact.origin != "wikilink"
    ]
    return semantic_contract.qualify_relation(fact, registry=registry, corpus=corpus).reasons


@pytest.mark.parametrize(
    ("relation", "reasons"),
    [
        ("supports", ()),
        ("refines", ()),
        ("answers", ()),
        ("contradicts", ()),
        ("supersedes", ()),
        ("x.backs", ()),
        ("x.qualifies", ()),
        ("x.retires", ()),
        ("links_to", ("excluded_family",)),
        ("cites", ("excluded_family",)),
        ("derived_from", ("excluded_family",)),
        ("evidenced_by", ("excluded_family",)),
        ("mentions", ("excluded_family",)),
        ("observed_in", ("excluded_family",)),
        ("x.see_also", ("excluded_family",)),
        ("x.based_on", ("excluded_family",)),
        ("relates_to", ("generic_relation",)),
        ("frobnicates", ("unregistered_relation",)),
    ],
)
def test_a_relation_row_is_a_typed_edge_unless_it_only_records_a_link(
    fixture_vault: Path, relation: str, reasons: tuple[str, ...]
) -> None:
    """Associative words and unregistered words are never counted as a claim about the target."""
    assert _qualification(fixture_vault, relation) == reasons  # CORE


def test_a_grandfathered_extension_is_a_typed_edge_by_its_saved_family(
    fixture_vault: Path,
) -> None:
    """FLIPS 9: `x.leans_on` is saved in family `support`, so it qualifies today.

    After intended change 9 it follows its parent `relates_to`, which is generic,
    and the reasons become `("generic_relation",)`.
    """
    assert _qualification(fixture_vault, "x.leans_on") == ()  # FLIPS 9


def _frontmatter_qualification(vault: Path, name: str) -> dict[str, tuple[str, ...]]:
    """Reasons per frontmatter field of one page, for facts that page authored."""
    registry = relation_registry.load_registry(vault)
    corpus = semantic_contract.build_corpus_context(vault, registry=registry)
    return {
        fact.authored_anchor: semantic_contract.qualify_relation(
            fact, registry=registry, corpus=corpus
        ).reasons
        for fact in corpus.relation_facts
        if fact.origin == "frontmatter" and fact.authored_path == f"{KB}/{name}.md"
    }


def test_only_a_supersession_field_is_a_typed_edge_in_frontmatter(fixture_vault: Path) -> None:
    reasons = _frontmatter_qualification(fixture_vault, "fm-all")

    # The supersession fields qualify on their own terms; the target pages here
    # are not superseded, so the pointer is judged as an ordinary typed edge.
    assert reasons["supersedes"] == ()  # CORE
    assert reasons["superseded_by"] == ()  # CORE
    for field in ("sources", "evidence", "evidences", "evidence_paths", "related"):
        assert "frontmatter_not_supersession" in reasons[field]  # CORE


def test_a_superseded_page_that_points_back_is_a_valid_supersession_target(
    fixture_vault: Path,
) -> None:
    """A mutual pointer pair qualifies in both directions; a one-way pointer does not."""
    assert _frontmatter_qualification(fixture_vault, "sup-new") == {"supersedes": ()}  # CORE
    assert _frontmatter_qualification(fixture_vault, "sup-old") == {"superseded_by": ()}  # CORE
    assert _frontmatter_qualification(fixture_vault, "sup-one-new") == {  # CORE
        "supersedes": ("ineligible_target",)
    }


# ---------------------------------------------------------------------------
# Supersession: currency, role review, the sensed model
# ---------------------------------------------------------------------------


def test_currency_follows_a_unit_row_that_supersedes_by_family(fixture_vault: Path) -> None:
    registry = relation_registry.load_registry(fixture_vault)
    rows = [
        {"kind": kind, "target": f"[[#{kind.replace('.', '-').replace('_', '-')}]]"}
        for kind in ("supersedes", "x.retires", "supports", "x.leans_on", "causes")
    ]

    targets = working_set_currency.relation_targets(rows, registry=registry)

    assert targets == ("[[#supersedes]]", "[[#x-retires]]")  # CORE


def test_role_review_collects_the_target_of_a_core_supersession_only(
    fixture_vault: Path,
) -> None:
    """FLIPS 4: the collector reads the key `supersedes`, so `x.retires` is not collected.

    After intended change 4 the extension is collected through its parent and
    `n2` lists `[[#u2]]`.
    """
    rel = f"{KB}/rs-page.md"
    state = semantic_contract.build_page_state(
        fixture_vault, rel, (fixture_vault / rel).read_text(encoding="utf-8")
    )

    described = artifact_role_state.describe(fixture_vault, state)

    superseding = {row["unit"]["anchor"]: row["supersedes"] for row in described["units"]}
    assert superseding["n1"] == ["[[#u1]]"]  # CORE
    assert superseding["n2"] == []  # FLIPS 4
    assert superseding["n3"] == []  # CORE


def _superseded_by(index: epistemic_graph.EpistemicGraphIndex, name: str) -> list[str]:
    """The pages the sensed model joins to one page by an authored supersession."""
    conn = index._open_read_snapshot()
    assert conn is not None
    try:
        facts = sensed_model.page_facts(
            conn, f"{KB}/{name}.md", epistemic_graph.GraphView(index.vault_root, conn)
        )
    finally:
        conn.close()
    assert facts is not None
    return sorted(_short(path) for path in facts.supersession)


def test_the_sensed_model_records_a_core_supersession_in_both_directions(
    fixture_vault: Path,
) -> None:
    index = build_graph(fixture_vault)

    assert _superseded_by(index, "rv-new") == ["rv-old-a"]  # CORE
    assert _superseded_by(index, "rv-old-a") == ["rv-new"]  # CORE
    assert _superseded_by(index, "sup-old") == ["sup-new"]  # CORE


def test_the_sensed_model_does_not_record_an_extension_of_supersedes(
    fixture_vault: Path,
) -> None:
    """FLIPS 4: the page fact reads the key `supersedes`, so `x.retires` joins no pair."""
    index = build_graph(fixture_vault)

    assert "rv-old-b" not in _superseded_by(index, "rv-new")  # FLIPS 4
    assert _superseded_by(index, "rv-old-b") == []  # FLIPS 4


# ---------------------------------------------------------------------------
# Census: structure checks that one authored edge can satisfy or break
#
# Each case builds its own two-page vault, so the counts name one edge.
# ---------------------------------------------------------------------------


def _census(tmp_path: Path, pages: dict[str, tuple[str, dict]]) -> dict:
    """The census of a small vault whose pages are `{name: (body, page options)}`."""
    root = tmp_path / "census"
    _write(root, "Knowledge Base/_Schema/relation-registry.yaml", OVERLAY)
    for name, (body, options) in pages.items():
        _page(root, name, body, **options)
    build_graph(root)
    return relation_census.census(root, keep=None)


def _check(census: dict, name: str) -> tuple[int, int]:
    """(edges the check applied to, edges it flagged)."""
    check = census["checks"][name]
    return check["applicable"], check["violations"]


def _row(relation: str, target: str) -> str:
    return f"## Relations\n- {relation} {_link(f'{KB}/{target}')}\n"


@pytest.mark.parametrize(
    ("newer", "expected"),
    [
        ("2026-01-01", (1, 1)),  # CORE: the replacing page is older than the page it replaces
        ("2026-03-01", (1, 0)),  # CORE
    ],
)
def test_the_census_flags_a_supersession_that_runs_backwards_in_time(
    tmp_path: Path, newer: str, expected: tuple[int, int]
) -> None:
    census = _census(
        tmp_path,
        {
            "new": (_row("supersedes", "old"), {"created": newer}),
            "old": ("An earlier view.", {"created": "2026-02-01"}),
        },
    )

    assert _check(census, "supersedes_backwards") == expected  # CORE


def test_the_census_does_not_date_check_an_extension_of_supersedes(tmp_path: Path) -> None:
    """FLIPS 4: the date check reads the key `supersedes`, so `x.retires` is not checked.

    After intended change 4 this is `(1, 1)`.
    """
    census = _census(
        tmp_path,
        {
            "new": (_row("x.retires", "old"), {"created": "2026-01-01"}),
            "old": ("An earlier view.", {"created": "2026-02-01"}),
        },
    )

    assert _check(census, "supersedes_backwards") == (0, 0)  # FLIPS 4


def test_the_census_flags_a_contradiction_inside_one_supersession_chain(tmp_path: Path) -> None:
    census = _census(
        tmp_path,
        {
            "a": (_row("contradicts", "b") + f"- supersedes {_link(f'{KB}/b')}\n", {}),
            "b": ("The other page.", {}),
        },
    )

    assert _check(census, "contradicts_within_chain") == (1, 1)  # CORE


def test_the_census_builds_no_chain_from_an_extension_of_supersedes(tmp_path: Path) -> None:
    """FLIPS 4: the chain reads the key `supersedes`; with `x.retires` the pair is not chained.

    After intended change 4 this is `(1, 1)`.
    """
    census = _census(
        tmp_path,
        {
            "a": (_row("contradicts", "b") + f"- x.retires {_link(f'{KB}/b')}\n", {}),
            "b": ("The other page.", {}),
        },
    )

    assert _check(census, "contradicts_within_chain") == (1, 0)  # FLIPS 4


def test_the_census_does_not_check_an_extension_of_contradicts_inside_a_chain(
    tmp_path: Path,
) -> None:
    """FLIPS 5: the chain check reads the key `contradicts`, so `x.disputes` is not checked.

    After intended change 5 this is `(1, 1)`.
    """
    census = _census(
        tmp_path,
        {
            "a": (_row("x.disputes", "b") + f"- supersedes {_link(f'{KB}/b')}\n", {}),
            "b": ("The other page.", {}),
        },
    )

    assert _check(census, "contradicts_within_chain") == (0, 0)  # FLIPS 5


def test_the_census_flags_evidence_that_points_at_a_page_that_is_no_evidence(
    tmp_path: Path,
) -> None:
    census = _census(
        tmp_path,
        {
            "a": (_row("evidenced_by", "b"), {}),
            "b": ("An ordinary insight.", {}),
            "c": (
                f"## Relations\n- evidenced_by {_link('Knowledge Base/Sources/doc')}\n",
                {},
            ),
            "doc": ("A document.", {"page_type": "source", "folder": "Knowledge Base/Sources"}),
        },
    )

    assert _check(census, "evidence_target_not_evidential") == (2, 1)  # CORE


def test_the_census_flags_an_answer_that_points_at_a_page_with_no_question(
    tmp_path: Path,
) -> None:
    census = _census(
        tmp_path,
        {
            "a": (_row("answers", "b"), {}),
            "b": ("An ordinary insight.", {}),
            "c": (_row("answers", "q"), {}),
            "q": (_unit("Open Question", "q1", text="Why?"), {}),
        },
    )

    assert _check(census, "answers_without_question") == (2, 1)  # CORE


@pytest.mark.parametrize("relation", ["supports", "x.backs"])
def test_the_census_flags_an_epistemic_relation_that_points_at_its_own_page(
    tmp_path: Path, relation: str
) -> None:
    """The check reads the family, so an extension in the same family is flagged too."""
    census = _census(tmp_path, {"a": (_row(relation, "a"), {})})

    assert _check(census, "same_page_epistemic") == (1, 1)  # CORE


def test_the_census_counts_a_plain_link_as_no_typed_page(tmp_path: Path) -> None:
    census = _census(tmp_path, {"a": (_row("links_to", "b"), {}), "b": ("The other page.", {})})

    assert census["metrics"]["typed_coverage"]["pages"] == 0  # CORE
    assert census["metrics"]["by_status"]["core_specific"] == 1  # CORE


def test_the_census_counts_an_extension_of_links_to_as_a_typed_page(tmp_path: Path) -> None:
    """FLIPS 6: the typed test excludes the key `links_to` only.

    After intended change 6 `x.see_also` is a plain link through its parent and
    the typed page count is 0.
    """
    census = _census(tmp_path, {"a": (_row("x.see_also", "b"), {}), "b": ("The other page.", {})})

    assert census["metrics"]["typed_coverage"]["pages"] == 1  # FLIPS 6
    assert census["metrics"]["by_status"]["extension"] == 1  # CORE


# ---------------------------------------------------------------------------
# Entity hydration, continued: contributors and authored links
# ---------------------------------------------------------------------------


def _contributors(vault: Path) -> list[str]:
    """The pages that count as newer facts about the `hub` entity."""
    context = dreamer_families.Context(vault_root=vault, store=None, conn=None, now=0.0)
    try:
        proposal = dreamer_families._hydration_detect(context, f"{ENTITIES}/hub.md")
    finally:
        context.close()
    assert proposal is not None
    return sorted(_short(path) for path in proposal["contributors"])


def test_a_page_that_supports_an_entity_contributes_and_one_derived_from_it_does_not(
    fixture_vault: Path,
) -> None:
    build_graph(fixture_vault)

    contributors = _contributors(fixture_vault)

    assert "ct-supports" in contributors  # CORE
    assert "ct-derived" not in contributors  # CORE


def test_a_page_that_cites_or_is_evidenced_by_an_entity_contributes(fixture_vault: Path) -> None:
    """FLIPS 11: only `derived_from` is skipped, so a citation or evidence edge contributes.

    After intended change 11 `ct-cites` and `ct-evidenced` leave the list.
    """
    build_graph(fixture_vault)

    contributors = _contributors(fixture_vault)

    assert "ct-cites" in contributors  # FLIPS 11
    assert "ct-evidenced" in contributors  # FLIPS 11


def test_a_page_that_is_based_on_an_entity_through_an_extension_contributes(
    fixture_vault: Path,
) -> None:
    """FLIPS 2: the skip reads the key `derived_from`, so `x.based_on` contributes today."""
    build_graph(fixture_vault)

    assert "ct-based" in _contributors(fixture_vault)  # FLIPS 2


def test_a_link_counts_as_authored_unless_it_is_a_derivation(fixture_vault: Path) -> None:
    """The linked subjects of `hy-page`: every entity it reaches except through `derived_from`."""
    build_graph(fixture_vault)
    context = dreamer_families.Context(vault_root=fixture_vault, store=None, conn=None, now=0.0)
    try:
        subjects = dreamer_families._linked_subjects(context, f"{KB}/hy-page.md")
    finally:
        context.close()

    linked = [_short(path) for path in subjects]

    assert "hy-supports" in linked  # CORE
    assert "hy-derived" not in linked  # CORE
    assert "hy-cites" in linked  # FLIPS 11
    assert "hy-evidenced" in linked  # FLIPS 11
    assert "hy-based" in linked  # FLIPS 2


# ---------------------------------------------------------------------------
# Catalogue membership, traversal profiles, the page claim
# ---------------------------------------------------------------------------


def _members(vault: Path) -> list[str]:
    status, edges = context_catalogue._membership_edges(vault)
    assert status == "available"
    return sorted(_short(source) for source, _target in edges)


def test_the_catalogue_reads_a_member_of_edge_and_its_extension_as_membership(
    fixture_vault: Path,
) -> None:
    build_graph(fixture_vault)

    members = _members(fixture_vault)

    assert "mem-core" in members  # CORE
    assert "mem-extension" in members  # CORE
    assert "mem-link" not in members  # CORE


def test_the_catalogue_reads_a_grandfathered_extension_as_membership_by_its_saved_family(
    fixture_vault: Path,
) -> None:
    """FLIPS 9: `x.joins` has parent `relates_to` and saved family `membership`.

    After intended change 9 it follows its parent and `mem-grandfathered` leaves
    the list.
    """
    build_graph(fixture_vault)

    assert "mem-grandfathered" in _members(fixture_vault)  # FLIPS 9


def test_the_built_in_traversal_profiles_keep_their_families_and_limits() -> None:
    profiles = traversal_profiles.builtin_profiles()

    assert {
        name: sorted(profile.families) for name, profile in profiles.items() if name != "all"
    } == {  # CORE
        "epistemic": [
            "answer",
            "contradiction",
            "duplication",
            "question",
            "refinement",
            "supersession",
            "support",
        ],
        "provenance": ["citation", "derivation", "evidence", "observation"],
        "causal": ["blocking", "causality", "dependency", "mitigation", "resolution"],
        "decision": [
            "dependency",
            "derivation",
            "evidence",
            "implementation",
            "mitigation",
            "resolution",
            "use",
        ],
    }
    for profile in profiles.values():
        assert profile.priority == tuple(sorted(profile.families))  # CORE
        assert (profile.direction, profile.include_extensions) == ("both", True)  # CORE
        assert (profile.max_depth, profile.max_nodes, profile.max_edges) == (5, 200, 400)  # CORE


def test_the_all_profile_holds_every_core_family() -> None:
    """The `order` and `assessment` families join this profile when the registry gains them."""
    registry = relation_registry.core_registry()

    profile = traversal_profiles.builtin_profiles(registry)["all"]

    assert profile.families == {definition.family for definition in registry.core.values()}  # CORE
    assert profile.families - {"order", "assessment"} == {  # CORE
        "affiliation",
        "answer",
        "blocking",
        "causality",
        "citation",
        "composition",
        "contradiction",
        "dependency",
        "derivation",
        "duplication",
        "entity",
        "evidence",
        "implementation",
        "kinship",
        "link",
        "location",
        "membership",
        "mention",
        "mitigation",
        "observation",
        "operation",
        "ownership",
        "production",
        "question",
        "refinement",
        "relation",
        "resolution",
        "specialization",
        "supersession",
        "supply",
        "support",
        "testing",
        "use",
    }


def test_a_claim_unit_is_the_page_claim_and_a_finding_unit_is_not() -> None:
    body = "## Finding\n- id: f1\n\nA finding body.\n\n## Claim\n- id: c1\n\nThe claim body.\n"

    claim = claims.extract_claim_text("Title", body)

    assert claim == "Title\n\nThe claim body."  # CORE
    assert "A finding body." not in (
        claims.extract_claim_text("Title", body.split("## Claim")[0]) or ""
    )  # CORE


# ---------------------------------------------------------------------------
# Unit corrections: which edges into a unit surface their source beside it
# ---------------------------------------------------------------------------

# Relations a source unit points at one target unit, to see which surface as a
# correction. The first eight are corrective today; the rest are not.
CORRECTING = (
    "contradicts",
    "refines",
    "supports",
    "supersedes",
    "resolves",
    "answers",
    "x.backs",
    "x.leans_on",
)
NOT_CORRECTING = (
    "relates_to",
    "links_to",
    "causes",
    "evidenced_by",
    "derived_from",
    "x.see_also",
    "frobnicates",
)


def _anchor(relation: str) -> str:
    return relation.replace(".", "-").replace("_", "-")


def _corrections(vault: Path) -> dict[str, str]:
    """For each relation, the one unit it corrects when its source is discovered."""
    relations = (*CORRECTING, *NOT_CORRECTING)
    target = f"{KB}/corr-target.md"
    _write(vault, "Knowledge Base/_Schema/relation-registry.yaml", OVERLAY)
    for rel, ident, units in (
        (
            target,
            1,
            "".join(
                _unit("Claim", f"t-{_anchor(r)}", text=f"Target claim for {r}.") for r in relations
            ),
        ),
        (
            f"{KB}/corr-source.md",
            2,
            "".join(
                _unit(
                    "Claim",
                    f"s-{_anchor(r)}",
                    f"{r}: [[{target.removesuffix('.md')}#t-{_anchor(r)}]]",
                    f"Source claim for {r}.",
                )
                for r in relations
            ),
        ),
    ):
        _write(
            vault,
            rel,
            f"---\ntype: insight\nstatus: active\ntitle: {Path(rel).stem}\n"
            f"exomem_id: {uuid.UUID(int=ident)}\n---\n# {Path(rel).stem}\n\n{units}",
        )
    # The live checkpoints a discovery reads, as a published graph leaves them.
    freshness.seed(
        vault,
        "vault",
        ((str(path), freshness.stat_signature(path)) for path in vault_module.walk_vault_md(vault)),
    )
    index = epistemic_graph.EpistemicGraphIndex(vault)
    find.evict_resolver_caches(vault)
    index.rebuild_all()
    find.recall_resolver_snapshot(
        vault, expected_checkpoint=freshness.live_recall_checkpoint(vault, "vault")
    )
    document = semantic_index.current_parent_index_state(vault, target).document
    refs = {r: document.resolve_fragment(f"t-{_anchor(r)}").unit.unit_ref for r in relations}
    found = unit_corrections.discover(vault, list(refs.values()), accept=lambda _row: True)
    assert found["status"] == "available"
    by_ref = {ref: relation for relation, ref in refs.items()}
    return {by_ref[unit["corrects"]]: unit["relation"] for unit in found["units"]}


def test_an_edge_into_a_unit_surfaces_its_source_when_its_relation_corrects(
    tmp_path: Path,
) -> None:
    corrections = _corrections(tmp_path / "vault")

    for relation in (
        "contradicts",
        "refines",
        "supports",
        "supersedes",
        "resolves",
        "answers",
        "x.backs",
    ):
        assert corrections[relation] == relation  # CORE
    for relation in NOT_CORRECTING:
        assert relation not in corrections  # CORE


def test_a_grandfathered_extension_corrects_by_its_saved_family(tmp_path: Path) -> None:
    """FLIPS 9: `x.leans_on` is saved in family `support`, so it corrects today.

    After intended change 9 it follows its parent `relates_to` and is absent.
    """
    assert "x.leans_on" in _corrections(tmp_path / "vault")  # FLIPS 9


def _pins_its_target(root: Path, relation: str) -> bool:
    """Whether an episode-style unit update that names one target accepts that relation.

    A corrective relation records the exact target it corrects, so the guard
    expects that fingerprint. Any other relation pins no target, and the same
    fingerprint is refused.
    """
    page = f"{KB}/guard.md"
    _write(root, "Knowledge Base/_Schema/relation-registry.yaml", OVERLAY)
    _write(
        root,
        page,
        f"---\ntype: insight\nstatus: active\ntitle: guard\nexomem_id: {uuid.UUID(int=7)}\n"
        "updated: 2026-07-15\n---\n# guard\n\n"
        + _unit("Claim", "old", text="The probe uses the old interval.")
        + _unit("Claim", "new", text="The probe uses the calibrated interval."),
    )
    with request_scope(owner_principal(surface="mcp")):
        old, new = semantic_index.current_parent_index_state(root, page).document.units
        try:
            commands.op_observe_memory(
                root,
                path=page,
                operation="update",
                unit_ref=new.unit_ref,
                expected_fingerprint=new.fingerprint,
                expected_hash=vault_module.content_hash((root / page).read_bytes().decode("utf-8")),
                kind="claim",
                category=new.category,
                content=new.content,
                relations=[{"kind": relation, "target": "[[#old]]"}],
                target_fingerprints={old.unit_ref: old.fingerprint},
            )
        except ValueError as error:
            assert "STALE_UNIT_TARGET" in str(error)
            return False
        return True


@pytest.mark.parametrize(
    ("relation", "pins"),
    [
        ("contradicts", True),  # CORE
        ("answers", True),  # CORE
        ("x.backs", True),  # CORE
        ("relates_to", False),  # CORE
        ("links_to", False),  # CORE
        ("evidenced_by", False),  # CORE
        ("x.see_also", False),  # CORE
    ],
)
def test_an_episode_correction_pins_the_exact_target_of_a_corrective_relation_only(
    tmp_path: Path, relation: str, pins: bool
) -> None:
    assert _pins_its_target(tmp_path / "vault", relation) is pins


def test_an_episode_correction_pins_the_target_of_a_grandfathered_extension(
    tmp_path: Path,
) -> None:
    """FLIPS 9: `x.leans_on` is a corrective family by its saved family today.

    After intended change 9 it follows its parent `relates_to` and pins nothing,
    so this returns False.
    """
    assert _pins_its_target(tmp_path / "vault", "x.leans_on") is True  # FLIPS 9


def test_a_possessive_turn_carries_only_claims_that_name_the_subject_by_about_entity(
    vault: Path,
) -> None:
    """Working-set admission reads the subject relation of each claim.

    A turn that asks about "Cargo Sled's quillon vantry constraint" admits a
    claim of an unrelated research note only when the claim says `about_entity`
    on the sled. Four claims differ only in the relation word; the page names
    no sled, so the relation alone decides.

    FLIPS 7: the `x.concerns` extension and its alias `concerns` do not admit
    today, because admission matches the key `about_entity`. After intended
    change 7 both admit, and the unregistered word still does not.
    """
    _seed_structure(vault)
    _seed_planning(vault)
    _seed_carry_pages(vault)
    _write(vault, "Knowledge Base/_Schema/relation-registry.yaml", OVERLAY)
    gauges = (
        ("core", "about_entity", "blue"),
        ("extension", "x.concerns", "amber"),
        ("alias", "concerns", "green"),
        ("unregistered", "x.unknown", "red"),
    )
    claims = "".join(
        f"## Claim\n- category: constraint\n- id: {name}\n"
        f"- relations: {word}: [[{SLED_ANCHOR}]]\n\n"
        f"The quillon vantry constraint uses the {gauge} gauge.\n\n"
        for name, word, gauge in gauges
    )
    _write(
        vault,
        CARRY_PAGE,
        "---\ntype: research-note\nstatus: active\n---\n# Workshop notes\n\n" + claims,
    )
    lexstore.ensure_fresh(vault)
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(vault).rebuild()
    token = request_budget.set_current(None)
    try:
        packet = working_set.compile_packet(
            vault, turn="Explain Cargo Sled's quillon vantry constraint.", max_chars=6000
        )
    finally:
        request_budget.reset_current(token)

    carried = {
        gauge
        for unit in packet["units"]
        if unit["provenance"]["path"] == CARRY_PAGE
        for _name, _word, gauge in gauges
        if f"{gauge} gauge" in unit["text"]
    }
    assert carried == {"blue"}  # CORE for blue; FLIPS 7 adds amber and green
