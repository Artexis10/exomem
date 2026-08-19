"""4b.32 — canonical names must not collide across unrelated scenarios.

Seed-1 drew 108 entities under 89 distinct names: 18 names shared by 37
entities, with 74 of 240 queries naming a colliding one. The sharpest
consequence was in ``t07_authority_conflict``, where three prompts were
byte-identical across two scenario instances while their expected values were
mutually exclusive — the corpus asked one question and graded two different
answers.

It was masked rather than harmless. The extractive answerer dumps whole
documents and ``gate_value`` matches by substring, so both values reached the
answer text and each query found its own. That cancellation is fragile in
exactly the direction the suite is heading: any narrowing of the answerer turns
these into real failures, and a published corpus containing one prompt with two
contradictory graded answers is indefensible whatever today's score happens to
be.

The invariant is deliberately **cross-scenario**, not global. ``t14`` is titled
"same-name people" and builds two distinct people under one name on purpose:
disambiguating them is what the identity family measures, and its expectations
are written for it. A name shared *inside* one scenario is that scenario's
subject; a name shared *between* scenarios is an accident that makes one
scenario's expectation silently apply to the other's entity.
"""

from __future__ import annotations

import collections
import json
from pathlib import Path

import pytest
from membench.generate import generate_corpus
from membench.templates import registry
from membench.templates.base import BuildContext


@pytest.fixture(scope="module")
def corpus(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("names") / "s1"
    generate_corpus(1, root)
    return root


def _rows(corpus: Path, name: str) -> list[dict]:
    # JSONL is UTF-8 by definition; a bare `read_text()` decodes with the
    # host's active code page and dies on the first name outside it.
    text = (corpus / name).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines()]


def test_no_canonical_name_is_shared_across_scenarios() -> None:
    """The 4b.32 invariant, asserted where scenario provenance still exists.

    The finished corpus does not record which template authored an entity, so
    this rebuilds the contexts rather than reading `entities.jsonl` — that is
    what lets it distinguish t14's deliberate pair from an accidental reuse.
    """

    owners: dict[str, set[tuple[str, int]]] = collections.defaultdict(set)
    for template_id, template in sorted(registry().items()):
        for variant in range(template.variants):
            ctx = BuildContext(template_id, variant, 1)
            template.build(ctx)
            for entity in ctx.graph.entities:
                owners[entity.canonical_name].add((template_id, variant))

    shared = {name: sorted(who) for name, who in owners.items() if len(who) > 1}
    assert not shared, shared


def test_no_prompt_grades_two_different_answers(corpus: Path) -> None:
    """The consequence that made this a release blocker rather than a tidy-up.

    Asserted over the finished corpus, because this is the property a third
    party can check against published bytes without rebuilding anything.
    """

    queries = {q["query_id"]: q for q in _rows(corpus, "queries.jsonl")}
    by_prompt: dict[str, set[str]] = collections.defaultdict(set)
    for expected in _rows(corpus, "expected.jsonl"):
        prompt = queries[expected["query_id"]]["prompt_text"]
        by_prompt[prompt].add(json.dumps(expected["answer"], sort_keys=True))

    conflicting = {p: sorted(v) for p, v in by_prompt.items() if len(v) > 1}
    assert not conflicting, conflicting


def test_the_identity_family_keeps_its_deliberate_same_name_pair(corpus: Path) -> None:
    """Guard against 'fixing' the defect by flattening what t14 measures.

    A uniqueness rule applied globally would rename one of t14's two people and
    quietly delete the family's subject, leaving a suite that looked clean and
    tested less.
    """

    ctx = BuildContext("t14_identity_graph", 0, 1)
    registry()["t14_identity_graph"].build(ctx)
    people = [e.canonical_name for e in ctx.graph.entities if e.kind == "person"]
    assert len(people) != len(set(people)), people


def test_name_spaces_have_room_for_every_context() -> None:
    """Allocation strides by context index, so a space smaller than the context
    count overflows on the very first slot rather than the last — which is how
    the 16-noun pool was caught. Keep the headroom explicit so adding templates
    fails here rather than in a corpus nobody re-reads."""

    from membench import wordbank

    contexts = sum(t.variants for t in registry().values())
    for space_name, needed_slots in (
        ("ORG_SPACE", 2),
        ("PROJECT_SPACE", 2),
        ("PERSON_SPACE", 3),
        ("CONCEPT_SPACE", 1),
    ):
        space = getattr(wordbank, space_name)
        assert space >= contexts * needed_slots, (space_name, space, contexts, needed_slots)
