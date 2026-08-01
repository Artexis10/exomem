"""The connectivity lane — a weaker sibling of the typed-relation predicate.

The write-time disposition was the only subsystem in the chain that could not see
a body wikilink: the retrieval graph already materialises them as
`links_to`/`origin: wikilink`, and the relation-debt audit already clears a page
that has them. These tests pin the third subsystem into line, and pin the three
things that must not break while doing it — the typed predicate's meaning, the
empty-corpus bootstrap carve-out, and non-vacuity.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import (
    memory_schema,
    relation_registry,
    semantic_contract,
    semantic_language_registry,
)


def _contracts() -> memory_schema.ResolvedMemoryContracts:
    return memory_schema.ResolvedMemoryContracts(
        validation="strict",
        matched_contracts=(("test", "Knowledge Base/_Schema/contracts/test.yaml"),),
        constraints=(),
        conflicts=(),
    )

_SOURCE_PAGE = "Knowledge Base/Sources/Articles/2026-05-04-example-capture.md"

# The minimum-unit gate is a separate, unrelated obligation. Pages under test
# carry one so a `missing_semantic_unit` block cannot be mistaken for a
# relation-disposition block.
_OBS = "## Observations\n\n- [constraint] Keep the boundary explicit.\n\n"


def _source(
    *,
    title: str = "Page",
    page_type: str | None = "insight",
    project: str | None = "atlas",
    status: str = "active",
    extra: str = "",
    body: str = "## Observations\n\n- [constraint] Keep the boundary explicit.\n",
) -> str:
    fields = [f"title: {title}", f"status: {status}"]
    if page_type is not None:
        fields.insert(1, f"type: {page_type}")
    if project is not None:
        fields.append(f"project: {project}")
    if extra:
        fields.extend(extra.rstrip().splitlines())
    return "---\n" + "\n".join(fields) + "\n---\n\n" + body


def _state(tmp_path: Path, rel_path: str, source: str) -> semantic_contract.SemanticPageState:
    return semantic_contract.build_page_state(
        tmp_path,
        rel_path,
        source,
        relation_registry=relation_registry.core_registry(),
        language_registry=semantic_language_registry.core_registry(),
        review_fingerprint="review-v1",
    )


def _corpus(tmp_path: Path, *states) -> semantic_contract.SemanticCorpusContext:
    return semantic_contract.SemanticCorpusContext.from_states(
        tmp_path,
        states,
        registry=relation_registry.core_registry(),
        identity_census=semantic_contract.StableIdentityCensus(
            tuple(
                semantic_contract.StableIdentityEntry(state.path, None)
                for state in states
            )
        ),
    )


def _target(tmp_path: Path) -> semantic_contract.SemanticPageState:
    return _state(
        tmp_path,
        "Knowledge Base/Notes/Patterns/target.md",
        _source(title="Target", page_type="pattern"),
    )


def _captured_source(tmp_path: Path) -> semantic_contract.SemanticPageState:
    return _state(
        tmp_path,
        _SOURCE_PAGE,
        "---\ntype: source\nsource_type: article\ncaptured: 2026-05-04\n"
        "ingested_into: []\n---\n\n# Example capture\n",
    )


def _disposition(tmp_path: Path, page, *others, operation: str = "edit", before=None):
    after_corpus = _corpus(tmp_path, page, *others)
    before_corpus = _corpus(tmp_path, *others) if before is None else after_corpus
    result = semantic_contract.evaluate(
        before=before,
        after=page,
        operation=operation,
        mode="precommit",
        before_contracts=_contracts(),
        after_contracts=_contracts(),
        before_corpus=before_corpus,
        after_corpus=after_corpus,
    )
    return result


# --------------------------------------------------------------------------
# The typed predicate must not change meaning.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ("cites", "evidenced_by", "derived_from", "links_to"))
def test_qualify_relation_still_rejects_every_excluded_family(
    tmp_path: Path, kind: str
) -> None:
    page = _state(
        tmp_path,
        "Knowledge Base/Notes/Insights/page.md",
        _source(body=f"## Relations\n- {kind} [[Target]]\n"),
    )
    corpus = _corpus(tmp_path, page, _target(tmp_path))

    qualified = semantic_contract.qualify_relation(
        corpus.outbound[page.path][0],
        registry=relation_registry.core_registry(),
        corpus=corpus,
    )

    assert not qualified.qualifies
    assert "excluded_family" in qualified.reasons


@pytest.mark.parametrize("kind", ("part_of", "contains", "extends"))
def test_composition_relations_qualify_as_typed_edges(
    tmp_path: Path, kind: str
) -> None:
    """Structural relations are real epistemic edges, not excluded families."""
    page = _state(
        tmp_path,
        "Knowledge Base/Notes/Insights/page.md",
        _source(body=f"## Relations\n- {kind} [[Knowledge Base/Notes/Patterns/target]]\n"),
    )

    result = _disposition(tmp_path, page, _target(tmp_path))

    assert result.relation_disposition.satisfied is True
    assert result.relation_disposition.qualifying_signal == "typed"
    codes = {finding.code for finding in result.findings}
    assert "RELATION_TYPED_EDGE_ABSENT" not in codes


def test_qualify_connectivity_accepts_what_the_typed_lane_excludes(
    tmp_path: Path,
) -> None:
    page = _state(
        tmp_path,
        "Knowledge Base/Notes/Insights/page.md",
        _source(body="## Relations\n- cites [[Target]]\n"),
    )
    corpus = _corpus(tmp_path, page, _target(tmp_path))
    fact = corpus.outbound[page.path][0]

    assert not semantic_contract.qualify_relation(
        fact, registry=relation_registry.core_registry(), corpus=corpus
    ).qualifies
    assert semantic_contract.qualify_connectivity(
        fact, registry=relation_registry.core_registry(), corpus=corpus
    ).qualifies


# --------------------------------------------------------------------------
# Body wikilinks become facts and satisfy the weaker lane.
# --------------------------------------------------------------------------


def test_body_wikilink_emits_a_links_to_fact_with_wikilink_origin(
    tmp_path: Path,
) -> None:
    page = _state(
        tmp_path,
        "Knowledge Base/Notes/Insights/page.md",
        _source(body=_OBS + "This builds on [[Knowledge Base/Notes/Patterns/target]].\n"),
    )
    corpus = _corpus(tmp_path, page, _target(tmp_path))

    facts = corpus.outbound[page.path]
    assert [fact.origin for fact in facts] == ["wikilink"]
    assert facts[0].canonical_relation == "links_to"


def test_body_wikilink_satisfies_as_connectivity_without_blocking(
    tmp_path: Path,
) -> None:
    page = _state(
        tmp_path,
        "Knowledge Base/Notes/Insights/page.md",
        _source(body=_OBS + "This builds on [[Knowledge Base/Notes/Patterns/target]].\n"),
    )

    result = _disposition(tmp_path, page, _target(tmp_path))

    assert result.relation_disposition.kind == "qualifying_relation"
    assert result.relation_disposition.satisfied is True
    assert result.relation_disposition.qualifying_signal == "connectivity"
    assert result.should_block is False

    codes = {finding.code for finding in result.findings}
    assert "RELATION_DISPOSITION_MISSING" not in codes
    warning = next(
        finding for finding in result.findings
        if finding.code == "RELATION_TYPED_EDGE_ABSENT"
    )
    assert warning.severity == "warning"


def test_typed_relation_reports_typed_and_emits_no_typed_edge_warning(
    tmp_path: Path,
) -> None:
    page = _state(
        tmp_path,
        "Knowledge Base/Notes/Insights/page.md",
        _source(body="## Relations\n- refines [[Knowledge Base/Notes/Patterns/target]]\n"),
    )

    result = _disposition(tmp_path, page, _target(tmp_path))

    assert result.relation_disposition.qualifying_signal == "typed"
    codes = {finding.code for finding in result.findings}
    assert "RELATION_TYPED_EDGE_ABSENT" not in codes


def test_cited_provenance_satisfies_connectivity_via_the_connectable_set(
    tmp_path: Path,
) -> None:
    captured = _captured_source(tmp_path)
    page = _state(
        tmp_path,
        "Knowledge Base/Notes/Insights/page.md",
        _source(extra=f'sources: ["[[{_SOURCE_PAGE.removesuffix(".md")}]]"]'),
    )
    corpus = _corpus(tmp_path, page, captured)

    # The Source is a legal connectivity target but never an eligible governed
    # page — that separation is what protects the bootstrap carve-out.
    assert captured.path in corpus.connectable_target_paths
    assert captured.path not in corpus.eligible_governed_paths

    result = _disposition(tmp_path, page, captured)
    assert result.relation_disposition.satisfied is True
    assert result.relation_disposition.qualifying_signal == "connectivity"


# --------------------------------------------------------------------------
# The three things that must not break.
# --------------------------------------------------------------------------


def test_inbound_links_and_backrefs_never_satisfy_connectivity(
    tmp_path: Path,
) -> None:
    """Outbound-only, or a page's own auto-written back-refs make it vacuous."""
    page = _state(
        tmp_path,
        "Knowledge Base/Notes/Insights/page.md",
        _source(body="No outbound connection at all.\n"),
    )
    citing = _state(
        tmp_path,
        "Knowledge Base/Notes/Patterns/citing.md",
        _source(
            title="Citing",
            page_type="pattern",
            body="Refers to [[Knowledge Base/Notes/Insights/page]].\n",
        ),
    )

    result = _disposition(tmp_path, page, citing)

    assert result.relation_disposition.satisfied is False
    assert result.relation_disposition.kind == "missing"


def test_empty_corpus_bootstrap_survives_a_captured_source(tmp_path: Path) -> None:
    """The cold-start guard: this fails if anyone merges the two target sets."""
    captured = _captured_source(tmp_path)
    first = _state(
        tmp_path,
        "Knowledge Base/Notes/Insights/first.md",
        _source(title="First", body=_OBS + "The very first compiled conclusion.\n"),
    )

    result = semantic_contract.evaluate(
        before=None,
        after=first,
        operation="create",
        mode="precommit",
        before_contracts=_contracts(),
        after_contracts=_contracts(),
        before_corpus=_corpus(tmp_path, captured),
        after_corpus=_corpus(tmp_path, first, captured),
    )

    assert result.relation_disposition.kind == "bootstrap"
    assert result.relation_disposition.satisfied is True
    assert result.should_block is False


def test_unresolved_wikilink_does_not_connect(tmp_path: Path) -> None:
    page = _state(
        tmp_path,
        "Knowledge Base/Notes/Insights/page.md",
        _source(body="Points at [[Knowledge Base/Notes/Patterns/does-not-exist]].\n"),
    )

    result = _disposition(tmp_path, page, _target(tmp_path))

    assert result.relation_disposition.satisfied is False


def test_wikilink_facts_are_deduped_and_capped_per_page(tmp_path: Path) -> None:
    repeated = "See [[Knowledge Base/Notes/Patterns/target]] again.\n" * 4
    many = "".join(
        f"Link [[Knowledge Base/Notes/Patterns/p{index}]].\n" for index in range(50)
    )
    page = _state(
        tmp_path,
        "Knowledge Base/Notes/Insights/page.md",
        _source(body=_OBS + repeated + many),
    )

    targets = [target for target, _ in page.body_wikilinks]
    assert len(targets) == len(set(targets)), "duplicate targets must collapse"
    assert len(targets) <= 32, "per-page fact cap must hold"


def test_typed_row_line_does_not_also_emit_a_wikilink_fact(tmp_path: Path) -> None:
    page = _state(
        tmp_path,
        "Knowledge Base/Notes/Insights/page.md",
        _source(body="## Relations\n- refines [[Knowledge Base/Notes/Patterns/target]]\n"),
    )
    corpus = _corpus(tmp_path, page, _target(tmp_path))

    origins = [fact.origin for fact in corpus.outbound[page.path]]
    assert "wikilink" not in origins
    assert len(origins) == 1
