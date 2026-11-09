"""What unit kinds do, before and after readers read a kind trait.

The change `make-epistemic-language-dynamic` moves the kind sets of five readers
into registry traits and the kinds themselves into a shipped pack. This module
began as the before-picture, in the style of `test_epistemic_language_parity`:
every `# CORE` value never changes, and a `# FLIPS <n>` value is the one that the
converting commit edited for intended change `<n>` of the change's design.md. The
comment on each flipped line names the value it held before. `# FLIPS protocol`
marks the role-review scenario that the same design lists beside change 12.

The vault registers three kinds. `research_question` and `protocol` are the
fixture's two vault kinds; each now gains what its trait names. `question` is a
vault kind that carries no trait, and is a control for the census no longer
matching its literal. The page claim of a core kind is recorded by
`test_epistemic_language_parity`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_artifact_role_projection import report, write
from test_artifact_role_review import method
from test_epistemic_language_parity import (
    KB,
    _check,
    _link,
    _page,
    _suggested,
    _unit,
    _write,
    build_graph,
)

from exomem import activation, relation_census, relation_registry, semantic_blocks, semantic_units
from exomem import semantic_language_registry as language_registry

REGISTRY = "Knowledge Base/_Schema/semantic-language-registry.yaml"

# A vault with the fixture's two kinds and one control. `question` carries no trait: the census
# once matched that literal, and the control proves it no longer does.
KIND_OVERLAY = """\
schema_version: 1
categories: {}
kinds:
  research_question:
    description: A question the research sets out to answer
    traits: [question]
  protocol:
    description: A repeatable protocol
    traits: [procedure]
  question: {description: A question under its shorter name}
"""


def _kind_vault(root: Path) -> Path:
    _write(root, REGISTRY, KIND_OVERLAY)
    return root


@pytest.fixture
def kind_vault(tmp_path: Path) -> Path:
    return _kind_vault(tmp_path / "vault")


# ---------------------------------------------------------------------------
# The kinds the code ships
# ---------------------------------------------------------------------------

# Migration guard: these copies of the kinds pack's tables retire with this change's archive.
# The 27 portable kinds and the compact `observation`, with the description
# every schema read shows for each.
CORE_KINDS = (
    "claim finding evidence decision assumption inference constraint risk open_question "
    "hypothesis prediction result metric failure pattern record case timeline_event "
    "requirement action definition procedure source experiment entity project media_segment"
).split()

# Every heading spelling that names a kind other than the kind's own key.
HEADING_SPELLINGS = {
    "claims": "claim",
    "findings": "finding",
    "proof": "evidence",
    "proofs": "evidence",
    "evidences": "evidence",
    "decisions": "decision",
    "assumptions": "assumption",
    "inferences": "inference",
    "constraints": "constraint",
    "risks": "risk",
    "open_questions": "open_question",
    "questions": "open_question",
    "hypotheses": "hypothesis",
    "predictions": "prediction",
    "results": "result",
    "outcome": "result",
    "outcomes": "result",
    "metrics": "metric",
    "failures": "failure",
    "patterns": "pattern",
    "records": "record",
    "cases": "case",
    "timeline": "timeline_event",
    "timelines": "timeline_event",
    "timeline_events": "timeline_event",
    "events": "timeline_event",
    "requirements": "requirement",
    "actions": "action",
    "todo": "action",
    "todos": "action",
    "definitions": "definition",
    "procedures": "procedure",
    "sources": "source",
    "experiments": "experiment",
    "entities": "entity",
    "projects": "project",
    "media_segments": "media_segment",
    "segments": "media_segment",
}


def test_the_core_registry_keeps_its_kinds_and_their_descriptions() -> None:
    core = language_registry.core_registry().core_kinds

    assert sorted(core) == sorted([*CORE_KINDS, "observation"])  # CORE
    assert core["observation"].description == "Compact semantic observation"  # CORE
    assert {
        key: definition.description for key, definition in core.items() if key != "observation"
    } == {key: f"Portable {key.replace('_', ' ')} semantic block" for key in CORE_KINDS}  # CORE


def test_every_heading_spelling_resolves_to_the_kind_it_named() -> None:
    resolved = {
        spelling: semantic_blocks.normalize_block_type(spelling) for spelling in HEADING_SPELLINGS
    }

    assert resolved == HEADING_SPELLINGS  # CORE
    assert {semantic_blocks.normalize_block_type(kind) for kind in CORE_KINDS} == set(  # CORE
        CORE_KINDS
    )


def test_a_plural_or_synonym_heading_resolves_to_open_question_and_the_singular_does_not() -> None:
    registry = language_registry.core_registry()

    for heading in ("Questions", "Open Questions", "Open_Questions", "Open Question"):
        assert registry.resolve_heading(heading).resolved == "open_question"  # CORE
    # `question` is no core spelling at all: only a vault can make it a kind.
    assert registry.resolve_heading("Question").resolved is None  # CORE


# ---------------------------------------------------------------------------
# Activation: which units count as assertions
# ---------------------------------------------------------------------------


def test_activation_counts_the_assertion_kinds_and_no_other(kind_vault: Path) -> None:
    body = "".join(
        _unit(heading, f"u{i}", text="A unit.")
        for i, heading in enumerate(
            ("Claim", "Finding", "Inference", "Hypothesis", "Result", "Decision", "Open Question")
        )
    )
    document = semantic_units.parse_semantic_units(body, validate=False)
    registry = relation_registry.load_registry(kind_vault)

    measured = activation.measure_document(
        document, registry, language=language_registry.core_registry(), project=None,
        page_type=None, body_wikilinks=0, frontmatter_counts={},
    )

    assert [unit.kind for unit in document.rich_units] == [
        "claim", "finding", "inference", "hypothesis", "result", "decision", "open_question",
    ]  # CORE
    assert measured["assertion_blocks"] == 5  # CORE


# ---------------------------------------------------------------------------
# The graph: pages that carry one question
# ---------------------------------------------------------------------------


def _question_pages(root: Path) -> None:
    """Three pairs, each carrying its own question text so that only a pair matches."""
    for name in ("oq-a", "oq-b"):
        _page(root, name, _unit("Open Question", "q1", text="Which store holds the sessions?"))
    # The plural and the synonym heading, which core resolves to `open_question`.
    _page(root, "syn-a", _unit("Questions", "q1", text="Who owns the retry policy?"))
    _page(root, "syn-b", _unit("Open Questions", "q1", text="Who owns the retry policy?"))
    for name in ("rq-a", "rq-b"):
        _page(root, name, _unit("Research Question", "q1", text="Why does the cache drift?"))


def test_two_pages_that_carry_one_question_are_proposed_as_related(kind_vault: Path) -> None:
    _question_pages(kind_vault)
    build_graph(kind_vault)

    assert _suggested(kind_vault, "oq-a", "shared_open_question") == [(None, "oq-b")]  # CORE
    assert _suggested(kind_vault, "syn-a", "shared_open_question") == [(None, "syn-b")]  # CORE
    assert _suggested(kind_vault, "rq-a", "shared_open_question") == [(None, "rq-b")]  # FLIPS 12: was []


# ---------------------------------------------------------------------------
# The census: what an `answers` edge may point at
# ---------------------------------------------------------------------------


def _answers(tmp_path: Path, heading: str, *, unit_target: bool) -> tuple[int, int]:
    """The `answers_without_question` check for one `answers` edge into a page or a unit."""
    root = _kind_vault(tmp_path / "census")
    _page(root, "target", _unit(heading, "q1", text="Why?"))
    row = f"## Relations\n- answers {_link(f'{KB}/target' + ('#q1' if unit_target else ''))}\n"
    _page(root, "source", row)
    build_graph(root)
    return _check(relation_census.census(root, keep=None), "answers_without_question")


@pytest.mark.parametrize("unit_target", [False, True], ids=["page", "unit"])
def test_an_answer_may_point_at_a_core_question_under_any_of_its_headings(
    tmp_path: Path, unit_target: bool
) -> None:
    for heading in ("Open Question", "Questions", "Open Questions"):
        assert _answers(tmp_path, heading, unit_target=unit_target) == (1, 0), heading  # CORE
    assert _answers(tmp_path, "Claim", unit_target=unit_target) == (1, 1)  # CORE


@pytest.mark.parametrize(
    ("unit_target", "research_question", "literal_question"),
    [
        # FLIPS 12: `research_question` was (1, 1) before it carried the `question` trait.
        # FLIPS 12: `question` was (1, 0) while the census read the literal.
        (False, (1, 0), (1, 1)),
        (True, (1, 0), (1, 1)),
    ],
    ids=["page", "unit"],
)
def test_an_answer_reads_a_vault_kind_by_its_trait_not_its_name(
    tmp_path: Path,
    unit_target: bool,
    research_question: tuple[int, int],
    literal_question: tuple[int, int],
) -> None:
    assert _answers(tmp_path, "Research Question", unit_target=unit_target) == research_question
    assert _answers(tmp_path, "Question", unit_target=unit_target) == literal_question


# ---------------------------------------------------------------------------
# Role review: which units are a method and its outcome
# ---------------------------------------------------------------------------


def _promotions(root: Path, body: str) -> int:
    write(root, body)
    return sum(1 for finding in report(root).findings if finding.category == "artifact_role_promotion")


def test_a_procedure_with_a_success_is_a_reusable_method(kind_vault: Path) -> None:
    assert _promotions(kind_vault, method()) == 1  # CORE


def test_a_protocol_is_a_reusable_method_only_when_it_has_the_procedure_trait(
    kind_vault: Path,
) -> None:
    protocol = method().replace("## Procedure", "## Protocol")

    assert _promotions(kind_vault, protocol) == 1  # FLIPS protocol: was 0
