from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from exomem import (
    activation_manifest,
    memory_schema,
    relation_registry,
    semantic_contract,
    semantic_language_registry,
    semantic_units,
)

_ID_A = "00000000-0000-0000-0000-000000000001"
_ID_B = "00000000-0000-0000-0000-000000000002"


def _source(
    *,
    title: str = "Page",
    page_type: str = "insight",
    project: str | None = "atlas",
    exomem_id: str | None = None,
    extra: str = "",
    body: str = "Body.\n",
) -> str:
    fields = [f"title: {title}", f"type: {page_type}", "status: active"]
    if project is not None:
        fields.append(f"project: {project}")
    if exomem_id is not None:
        fields.append(f"exomem_id: {exomem_id}")
    if extra:
        fields.extend(extra.rstrip().splitlines())
    return "---\n" + "\n".join(fields) + "\n---\n\n" + body


def _state(
    tmp_path: Path,
    rel_path: str,
    source: str,
    *,
    review_fingerprint: str | None = "review-v1",
) -> semantic_contract.SemanticPageState:
    return semantic_contract.build_page_state(
        tmp_path,
        rel_path,
        source,
        relation_registry=relation_registry.core_registry(),
        language_registry=semantic_language_registry.core_registry(),
        review_fingerprint=review_fingerprint,
    )


def _corpus(
    tmp_path: Path, *states: semantic_contract.SemanticPageState
) -> semantic_contract.SemanticCorpusContext:
    return semantic_contract.SemanticCorpusContext.from_states(
        tmp_path,
        states,
        registry=relation_registry.core_registry(),
        identity_census=_identity_census(*states),
    )


def _identity_census(
    *states: semantic_contract.SemanticPageState,
) -> semantic_contract.StableIdentityCensus:
    return semantic_contract.StableIdentityCensus(
        tuple(
            semantic_contract.StableIdentityEntry(
                state.path,
                state.identity if state.identity_kind == "exomem_id" else None,
            )
            for state in states
        )
    )


def _contracts(
    *constraints: memory_schema.ResolvedContractConstraint,
    validation: str = "strict",
    conflicts: tuple[memory_schema.ContractResolutionConflict, ...] = (),
) -> memory_schema.ResolvedMemoryContracts:
    return memory_schema.ResolvedMemoryContracts(
        validation=validation,
        matched_contracts=(("test", "Knowledge Base/_Schema/contracts/test.yaml"),),
        constraints=tuple(constraints),
        conflicts=conflicts,
    )


def _constraint(
    identity: tuple[str, str, str], value
) -> memory_schema.ResolvedContractConstraint:
    return memory_schema.ResolvedContractConstraint(
        namespace=identity[0],
        element=identity[1],
        constraint=identity[2],
        value=value,
        specificity="global",
        contracts=("test",),
        provenance=(
            (
                "test",
                "Knowledge Base/_Schema/contracts/test.yaml",
                identity[1],
                "global",
            ),
        ),
    )


def _evaluate(
    *,
    before: semantic_contract.SemanticPageState | None,
    after: semantic_contract.SemanticPageState,
    before_corpus: semantic_contract.SemanticCorpusContext,
    after_corpus: semantic_contract.SemanticCorpusContext,
    contracts: memory_schema.ResolvedMemoryContracts | None = None,
    operation: str = "edit",
    mode: str = "precommit",
    before_review: semantic_contract.RelationReviewState | None = None,
    after_review: semantic_contract.RelationReviewState | None = None,
    grandfathered: bool = False,
) -> semantic_contract.SemanticContractResult:
    empty = _contracts()
    return semantic_contract.evaluate(
        before=before,
        after=after,
        operation=operation,
        mode=mode,
        before_contracts=contracts or empty,
        after_contracts=contracts or empty,
        before_corpus=before_corpus,
        after_corpus=after_corpus,
        before_review=before_review,
        after_review=after_review,
        grandfathered=grandfathered,
    )


def test_page_state_is_immutable_detached_and_deterministic(tmp_path: Path) -> None:
    source = _source(
        exomem_id=_ID_A,
        extra="projects: [companion, atlas]\nflags: [one, two]",
        body="- [Config] Value\n",
    )
    state = _state(tmp_path, "Knowledge Base/Notes/Insights/page.md", source)

    assert state.identity_kind == "exomem_id"
    assert state.identity == _ID_A
    assert state.projects == ("atlas", "companion")
    assert state.eligible_governed and state.eligible_compiled
    assert state.as_dict() == state.as_dict()
    with pytest.raises(FrozenInstanceError):
        state.path = "changed.md"  # type: ignore[misc]
    with pytest.raises(TypeError):
        state.frontmatter["title"] = "Changed"  # type: ignore[index]
    with pytest.raises(TypeError):
        state.frontmatter["flags"][0] = "changed"  # type: ignore[index]


def test_review_fingerprint_default_is_normalized_but_source_hash_stays_raw(
    tmp_path: Path,
) -> None:
    raw = _source(exomem_id=_ID_A, body="Body.\r\n")
    normalized = raw.replace("\r\n", "\n")
    defaulted = semantic_contract.build_page_state(
        tmp_path,
        "Knowledge Base/Notes/Insights/page.md",
        raw,
        relation_registry=relation_registry.core_registry(),
        language_registry=semantic_language_registry.core_registry(),
    )
    normalized_state = semantic_contract.build_page_state(
        tmp_path,
        "Knowledge Base/Notes/Insights/page.md",
        normalized,
        relation_registry=relation_registry.core_registry(),
        language_registry=semantic_language_registry.core_registry(),
    )
    suppressed = semantic_contract.build_page_state(
        tmp_path,
        "Knowledge Base/Notes/Insights/page.md",
        raw,
        relation_registry=relation_registry.core_registry(),
        language_registry=semantic_language_registry.core_registry(),
        review_fingerprint=None,
    )

    assert defaulted.source_hash == semantic_contract.vault.content_hash(raw)
    assert defaulted.source_hash != normalized_state.source_hash
    assert defaulted.review_fingerprint == normalized_state.review_fingerprint
    assert defaulted.review_fingerprint is not None
    assert suppressed.review_fingerprint is None


def test_broad_identity_census_prevents_direct_review_state_bypass(
    tmp_path: Path,
) -> None:
    page = _state(
        tmp_path,
        "Knowledge Base/Notes/Insights/page.md",
        _source(exomem_id=_ID_A, body="## Relations\n"),
        review_fingerprint="fingerprint",
    )
    census = semantic_contract.StableIdentityCensus(
        (
            semantic_contract.StableIdentityEntry(page.path, _ID_A),
            semantic_contract.StableIdentityEntry(
                "Knowledge Base/Private/page.sync-conflict-copy.MD", _ID_A
            ),
        )
    )
    corpus = semantic_contract.SemanticCorpusContext.from_states(
        tmp_path,
        (page,),
        registry=relation_registry.core_registry(),
        identity_census=census,
    )
    review = semantic_contract.RelationReviewState(
        "reviewed_none", _ID_A, "fingerprint", reason="reviewed"
    )

    result = _evaluate(
        before=page,
        after=page,
        before_corpus=corpus,
        after_corpus=corpus,
        after_review=review,
    )

    assert result.relation_disposition.kind == "stale"
    assert result.should_block


def test_page_state_preserves_non_string_frontmatter_key_identity(
    tmp_path: Path,
) -> None:
    state = _state(
        tmp_path,
        "Knowledge Base/Notes/Insights/keys.md",
        _source(extra='1: numeric\n"1": string'),
    )

    assert state.frontmatter[1] == "numeric"
    assert state.frontmatter["1"] == "string"
    assert state.as_dict()["frontmatter"][1] == "numeric"


def test_build_corpus_reads_and_parses_each_page_once_and_reuses_pure_census(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for index in range(2):
        path = tmp_path / f"Knowledge Base/Notes/Insights/{index}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_source(title=str(index)), encoding="utf-8")
    reads: dict[str, int] = {}
    parses = 0
    original_read = Path.read_text
    original_parse = semantic_contract.semantic_units.parse_semantic_units

    def counted_read(path: Path, *args, **kwargs):
        if path.suffix == ".md":
            reads[path.as_posix()] = reads.get(path.as_posix(), 0) + 1
        return original_read(path, *args, **kwargs)

    def counted_parse(*args, **kwargs):
        nonlocal parses
        parses += 1
        return original_parse(*args, **kwargs)

    def forbidden_census(*_args, **_kwargs):
        raise AssertionError("corpus context must use the pure census factory")

    monkeypatch.setattr(Path, "read_text", counted_read)
    monkeypatch.setattr(semantic_contract.semantic_units, "parse_semantic_units", counted_parse)
    monkeypatch.setattr(activation_manifest, "build_census", forbidden_census)

    corpus = semantic_contract.build_corpus_context(tmp_path)

    assert parses == 2
    assert set(reads.values()) == {1}
    assert len(corpus.activation_census.candidates) == 2


def test_candidate_insertion_reresolves_forward_and_ambiguous_facts_without_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _state(
        tmp_path,
        "Knowledge Base/Notes/Insights/source.md",
        _source(body="## Relations\n- supports [[target]]\n"),
    )
    first_target = _state(
        tmp_path,
        "Knowledge Base/Notes/Insights/target.md",
        _source(title="Other", body="Body.\n"),
    )
    missing_context = _corpus(tmp_path, source)
    unique_context = missing_context.with_candidate(first_target)
    assert unique_context.outbound[source.path][0].target_status == "resolved"

    duplicate = _state(
        tmp_path,
        "Knowledge Base/Notes/Patterns/target.md",
        _source(title="Duplicate", page_type="pattern"),
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("candidate patching must not touch disk or parse")

    monkeypatch.setattr(Path, "read_text", forbidden)
    monkeypatch.setattr(semantic_units, "parse_semantic_units", forbidden)
    ambiguous = unique_context.with_candidate(duplicate)

    fact = ambiguous.outbound[source.path][0]
    assert fact.target_status == "ambiguous"
    assert first_target.path not in ambiguous.inbound


def test_evaluate_fails_closed_when_after_corpus_does_not_match_after_state(
    tmp_path: Path,
) -> None:
    target = _state(
        tmp_path,
        "Knowledge Base/Notes/Patterns/target.md",
        _source(title="Target", page_type="pattern"),
    )
    before = _state(
        tmp_path,
        "Knowledge Base/Notes/Insights/page.md",
        _source(body="## Relations\n- supports [[Target]]\n"),
    )
    after = _state(
        tmp_path,
        before.path,
        _source(body="## Relations\n"),
    )
    stale_corpus = _corpus(tmp_path, before, target)

    result = _evaluate(
        before=before,
        after=after,
        before_corpus=stale_corpus,
        after_corpus=stale_corpus,
    )

    assert result.relation_disposition.kind == "missing"
    assert result.should_block
    assert any(
        finding.code == "SEMANTIC_CORPUS_STATE_MISMATCH"
        for finding in result.errors
    )
    assert not result.relation_disposition.qualifying_facts


def test_evaluate_fails_closed_when_before_corpus_does_not_match_before_state(
    tmp_path: Path,
) -> None:
    before = _state(
        tmp_path,
        "Knowledge Base/Notes/Insights/page.md",
        _source(body="## Relations\n"),
    )
    stale_before = _state(
        tmp_path,
        before.path,
        _source(body="## Relations\n- custom.rel [[Missing]]\n"),
    )
    after = _state(
        tmp_path,
        before.path,
        _source(body="Changed.\n\n## Relations\n- custom.rel [[Missing]]\n"),
    )
    stale_before_corpus = _corpus(tmp_path, stale_before)
    after_corpus = _corpus(tmp_path, after)

    result = _evaluate(
        before=before,
        after=after,
        before_corpus=stale_before_corpus,
        after_corpus=after_corpus,
        grandfathered=True,
    )

    assert result.should_block
    assert any(
        finding.code == "SEMANTIC_CORPUS_STATE_MISMATCH"
        for finding in result.blocking_findings
    )
    unregistered = [
        finding
        for finding in result.findings
        if finding.code == "unregistered_relation"
    ]
    assert len(unregistered) == 1
    assert unregistered[0].severity == "error"


def test_outside_kb_pages_remain_resolvable_but_never_governed(
    tmp_path: Path,
) -> None:
    outside_compiled = _state(
        tmp_path,
        "Outside/compiled.md",
        _source(title="Outside Compiled", page_type="insight"),
    )
    outside_entity = _state(
        tmp_path,
        "Outside/entity.md",
        _source(title="Outside Entity", page_type="entity"),
    )
    candidate = _state(
        tmp_path,
        "Knowledge Base/Notes/Insights/candidate.md",
        _source(body="## Relations\n- supports [[Outside Entity]]\n"),
    )
    before_corpus = _corpus(tmp_path, outside_compiled, outside_entity)
    after_corpus = _corpus(tmp_path, outside_compiled, outside_entity, candidate)
    fact = after_corpus.outbound[candidate.path][0]

    qualification = semantic_contract.qualify_relation(
        fact,
        registry=relation_registry.core_registry(),
        corpus=after_corpus,
    )
    result = _evaluate(
        before=None,
        after=candidate,
        before_corpus=before_corpus,
        after_corpus=after_corpus,
        operation="create",
    )

    assert outside_compiled.path in after_corpus.pages
    assert outside_entity.path in after_corpus.pages
    assert fact.target_status == "resolved"
    assert not outside_compiled.eligible_compiled
    assert not outside_entity.eligible_governed
    assert after_corpus.eligible_governed_paths == frozenset({candidate.path})
    assert [item.rel_path for item in after_corpus.activation_census.candidates] == [
        candidate.path
    ]
    assert not qualification.qualifies
    assert "ineligible_target" in qualification.reasons
    assert result.relation_disposition.kind == "bootstrap"


def test_relation_qualification_outbound_inbound_entity_and_self_rejection(
    tmp_path: Path,
) -> None:
    source = _state(
        tmp_path,
        "Knowledge Base/Notes/Insights/source.md",
        _source(body="## Relations\n- supports [[Target#claim]]\n"),
    )
    target = _state(
        tmp_path,
        "Knowledge Base/Entities/target.md",
        _source(title="Target", page_type="entity"),
    )
    corpus = _corpus(tmp_path, source, target)
    fact = corpus.outbound[source.path][0]
    qualified = semantic_contract.qualify_relation(
        fact, registry=relation_registry.core_registry(), corpus=corpus
    )

    assert qualified.qualifies is True
    assert fact.origin == "markdown_relation"
    assert fact.resolved_target_path.endswith("target.md#claim")
    assert corpus.inbound[target.path][0] == fact

    self_page = _state(
        tmp_path,
        "Knowledge Base/Notes/Insights/self.md",
        _source(title="Self", body="## Relations\n- supports [[Self]]\n"),
    )
    self_corpus = _corpus(tmp_path, self_page)
    self_result = semantic_contract.qualify_relation(
        self_corpus.outbound[self_page.path][0],
        registry=relation_registry.core_registry(),
        corpus=self_corpus,
    )
    assert self_result.reasons == ("self_target",)


def test_inbound_relation_requires_eligible_opposite_logical_endpoint(
    tmp_path: Path,
) -> None:
    source = _state(
        tmp_path,
        "Knowledge Base/Sources/raw.md",
        _source(
            title="Raw",
            page_type="source",
            body="## Relations\n- supports [[Target]]\n",
        ),
    )
    target = _state(
        tmp_path,
        "Knowledge Base/Notes/Insights/target.md",
        _source(title="Target"),
    )
    corpus = _corpus(tmp_path, source, target)
    fact = corpus.inbound[target.path][0]

    qualified = semantic_contract.qualify_relation(
        fact, registry=relation_registry.core_registry(), corpus=corpus
    )
    result = _evaluate(
        before=target,
        after=target,
        before_corpus=corpus,
        after_corpus=corpus,
    )

    assert not source.eligible_governed
    assert not qualified.qualifies
    assert "ineligible_target" in qualified.reasons
    assert result.relation_disposition.kind == "missing"


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("links_to", "excluded_family"),
        ("cites", "excluded_family"),
        ("derived_from", "excluded_family"),
        ("evidenced_by", "excluded_family"),
        ("mentions", "excluded_family"),
        ("observed_in", "excluded_family"),
    ],
)
def test_excluded_relation_families_never_qualify(
    tmp_path: Path, kind: str, expected: str
) -> None:
    source = _state(
        tmp_path,
        "Knowledge Base/Notes/Insights/source.md",
        _source(body=f"## Relations\n- {kind} [[Target]]\n"),
    )
    target = _state(
        tmp_path,
        "Knowledge Base/Notes/Patterns/target.md",
        _source(title="Target", page_type="pattern"),
    )
    corpus = _corpus(tmp_path, source, target)

    qualified = semantic_contract.qualify_relation(
        corpus.outbound[source.path][0],
        registry=relation_registry.core_registry(),
        corpus=corpus,
    )

    assert expected in qualified.reasons


def test_attached_projects_use_any_valid_scope_and_superseded_by_keeps_authored_scope(
    tmp_path: Path,
) -> None:
    registry = relation_registry.load_registry(
        proposal={
            "schema_version": 1,
            "extensions": {
                "science.replicates": {
                    "parent": "supports",
                    "description": "Replicates",
                    "scope": {"projects": ["companion"]},
                }
            },
        }
    )
    authored = _state(
        tmp_path,
        "Knowledge Base/Notes/Insights/current.md",
        _source(
            project=None,
            extra="projects: [atlas, companion]\nsuperseded_by: '[[Older]]'",
            body="## Relations\n- science.replicates [[Older]]\n",
        ),
    )
    older = _state(
        tmp_path,
        "Knowledge Base/Notes/Patterns/older.md",
        _source(title="Older", page_type="pattern", project="other"),
    )
    corpus = semantic_contract.SemanticCorpusContext.from_states(
        tmp_path,
        (authored, older),
        registry=registry,
        identity_census=_identity_census(authored, older),
    )
    extension, lifecycle = corpus.outbound[authored.path][0], corpus.outbound[older.path][0]

    assert semantic_contract.qualify_relation(extension, registry=registry, corpus=corpus).qualifies
    assert lifecycle.authored_path == authored.path
    assert lifecycle.logical_source_path == older.path
    assert lifecycle.logical_target_path == authored.path


def test_project_scoped_relation_requires_an_attached_authored_project(
    tmp_path: Path,
) -> None:
    registry = relation_registry.load_registry(
        proposal={
            "schema_version": 1,
            "extensions": {
                "science.replicates": {
                    "parent": "supports",
                    "description": "Replicates",
                    "scope": {"projects": ["alpha"]},
                }
            },
        }
    )
    authored = _state(
        tmp_path,
        "Knowledge Base/Notes/Insights/current.md",
        _source(
            project=None,
            body="## Relations\n- science.replicates [[Target]]\n",
        ),
    )
    target = _state(
        tmp_path,
        "Knowledge Base/Notes/Patterns/target.md",
        _source(title="Target", page_type="pattern", project="alpha"),
    )
    corpus = semantic_contract.SemanticCorpusContext.from_states(
        tmp_path,
        (authored, target),
        registry=registry,
        identity_census=_identity_census(authored, target),
    )
    fact = corpus.outbound[authored.path][0]

    qualified = semantic_contract.qualify_relation(
        fact, registry=registry, corpus=corpus
    )

    assert authored.projects == ()
    assert not qualified.qualifies
    assert "scope_violation" in qualified.reasons


def test_relation_scope_uses_graph_file_target_kind_and_preserves_raw_alias(
    tmp_path: Path,
) -> None:
    registry = relation_registry.load_registry(
        proposal={
            "schema_version": 1,
            "extensions": {
                "science.replicates": {
                    "parent": "supports",
                    "description": "Replicates",
                    "aliases": ["replicates"],
                    "source_kinds": ["file"],
                    "target_kinds": ["file"],
                }
            },
        }
    )
    source = _state(
        tmp_path,
        "Knowledge Base/Notes/Insights/source.md",
        _source(body="## Relations\n- replicates [[Target]]\n"),
    )
    target = _state(
        tmp_path,
        "Knowledge Base/Notes/Patterns/target.md",
        _source(title="Target", page_type="pattern"),
    )
    corpus = semantic_contract.SemanticCorpusContext.from_states(
        tmp_path,
        (source, target),
        registry=registry,
        identity_census=_identity_census(source, target),
    )
    fact = corpus.outbound[source.path][0]

    assert fact.raw_relation == "replicates"
    assert fact.canonical_relation == "science.replicates"
    assert semantic_contract.qualify_relation(
        fact, registry=registry, corpus=corpus
    ).qualifies


def test_schema_constraints_exact_types_namespaces_unknowns_and_modes(
    tmp_path: Path,
) -> None:
    state = _state(
        tmp_path,
        "Knowledge Base/Notes/Insights/page.md",
        _source(
            extra="flag: true\ncount: 1\nratio: 1.0\npublished: 2026-01-01\nextra_field: yes",
            body=(
                "- [Config] Compact\n\n"
                "## Finding\n- category: rule\nRich.\n\n"
                "## Relations\n- supports [[Missing]]\n"
            ),
        ),
    )
    corpus = _corpus(tmp_path, state)
    contracts = _contracts(
        _constraint(("fields", "flag", "types"), ("integer",)),
        _constraint(("fields", "count", "enum"), (True,)),
        _constraint(("fields", "missing", "required"), True),
        _constraint(("blocks", "finding", "required"), True),
        _constraint(("kinds", "observation", "required"), True),
        _constraint(("categories", "config", "required"), True),
        _constraint(("relations", "supports", "required"), True),
        _constraint(("fields", "*", "allowed"), ("flag", "count", "ratio", "published")),
        _constraint(("blocks", "*", "allowed"), ("finding",)),
        _constraint(("kinds", "*", "allowed"), ("finding", "observation")),
        _constraint(("categories", "*", "allowed"), ("config", "rule")),
        _constraint(("relations", "*", "allowed"), ("supports",)),
    )

    strict = _evaluate(
        before=state,
        after=state,
        before_corpus=corpus,
        after_corpus=corpus,
        contracts=contracts,
    )
    warn = semantic_contract.evaluate(
        before=state,
        after=state,
        operation="edit",
        mode="precommit",
        before_contracts=replace(contracts, validation="warn"),
        after_contracts=replace(contracts, validation="warn"),
        before_corpus=corpus,
        after_corpus=corpus,
    )
    off = semantic_contract.evaluate(
        before=state,
        after=state,
        operation="edit",
        mode="precommit",
        before_contracts=replace(contracts, validation="off"),
        after_contracts=replace(contracts, validation="off"),
        before_corpus=corpus,
        after_corpus=corpus,
    )

    schema_codes = {
        finding.code
        for finding in strict.findings
        if finding.code.startswith("CONTRACT_")
    }
    assert {
        "CONTRACT_FIELD_TYPE",
        "CONTRACT_FIELD_ENUM",
        "CONTRACT_REQUIRED_FIELD",
        "CONTRACT_UNKNOWN_FIELD",
    } <= schema_codes
    assert not any(
        finding.resolved_rule == ("relations", "supports", "required")
        for finding in strict.findings
    )
    assert strict.errors
    assert warn.warnings and not any(
        finding.code == "CONTRACT_FIELD_TYPE" for finding in warn.errors
    )
    assert not any(f.code.startswith("CONTRACT_") for f in off.findings)


def test_syntax_conflicts_and_relation_presence_are_independent_of_endpoint(
    tmp_path: Path,
) -> None:
    state = _state(
        tmp_path,
        "Knowledge Base/Notes/Insights/page.md",
        _source(body="## Relations\n- supports [[Missing]]\n- (none yet)\n"),
    )
    corpus = _corpus(tmp_path, state)
    conflict = memory_schema.ContractResolutionConflict(
        code="CONTRACT_RULE_CONFLICT",
        resolved_rule=("fields", "status", "types"),
        contracts=("test",),
        detail="conflict",
    )
    contracts = _contracts(
        _constraint(("relations", "supports", "required"), True),
        validation="off",
        conflicts=(conflict,),
    )

    result = _evaluate(
        before=state,
        after=state,
        before_corpus=corpus,
        after_corpus=corpus,
        contracts=contracts,
    )

    assert any(f.resolved_rule == ("relations", "*", "syntax") for f in result.errors)
    assert any(f.code == "CONTRACT_RULE_CONFLICT" for f in result.errors)
    assert not any(
        finding.resolved_rule == ("relations", "supports", "required")
        for finding in result.findings
    )


def test_stable_malformed_key_ignores_unrelated_line_shift(tmp_path: Path) -> None:
    first = _state(
        tmp_path,
        "Knowledge Base/Notes/Insights/page.md",
        _source(body="## Relations\n- (none yet)\n"),
    )
    shifted = _state(
        tmp_path,
        first.path,
        _source(body="Unrelated.\n\n## Relations\n- (none yet)\n"),
    )
    first_corpus = _corpus(tmp_path, first)
    shifted_corpus = _corpus(tmp_path, shifted)

    first_result = _evaluate(
        before=first,
        after=first,
        before_corpus=first_corpus,
        after_corpus=first_corpus,
    )
    shifted_result = _evaluate(
        before=shifted,
        after=shifted,
        before_corpus=shifted_corpus,
        after_corpus=shifted_corpus,
    )

    first_key = next(f.key for f in first_result.findings if f.code == "malformed_relation")
    shifted_key = next(f.key for f in shifted_result.findings if f.code == "malformed_relation")
    assert first_key == shifted_key


def test_relation_fact_and_debt_identity_survive_line_shift_and_target_resolution(
    tmp_path: Path,
) -> None:
    before = _state(
        tmp_path,
        "Knowledge Base/Notes/Insights/page.md",
        _source(body="## Relations\n- custom.rel [[Missing]]\n"),
    )
    after = _state(
        tmp_path,
        before.path,
        _source(body="Unrelated.\n\n## Relations\n- custom.rel [[Missing]]\n"),
    )
    target = _state(
        tmp_path,
        "Knowledge Base/Notes/Patterns/missing.md",
        _source(title="Missing", page_type="pattern"),
    )
    before_corpus = _corpus(tmp_path, before)
    after_corpus = _corpus(tmp_path, after, target)
    before_fact = before_corpus.outbound[before.path][0]
    after_fact = after_corpus.outbound[after.path][0]

    result = _evaluate(
        before=before,
        after=after,
        before_corpus=before_corpus,
        after_corpus=after_corpus,
        grandfathered=True,
    )

    assert before_fact.target_status == "unresolved"
    assert after_fact.target_status == "resolved"
    assert before_fact.authored_line != after_fact.authored_line
    assert before_fact.identity == after_fact.identity
    debt = [
        finding
        for finding in result.findings
        if finding.code == "unregistered_relation"
    ]
    assert len(debt) == 1
    assert debt[0].severity == "warning"
    assert not result.should_block


def test_anonymous_rich_relation_fact_identity_uses_unit_binding_not_line_number(
    tmp_path: Path,
) -> None:
    before = _state(
        tmp_path,
        "Knowledge Base/Notes/Insights/page.md",
        _source(
            body=(
                "## Finding\n"
                "- relations: custom.rel: [[Missing]]\n\n"
                "Finding body.\n"
            )
        ),
    )
    after = _state(
        tmp_path,
        before.path,
        _source(
            body=(
                "Unrelated.\n\n"
                "## Finding\n"
                "- relations: custom.rel: [[Missing]]\n\n"
                "Finding body.\n"
            )
        ),
    )
    target = _state(
        tmp_path,
        "Knowledge Base/Notes/Patterns/missing.md",
        _source(title="Missing", page_type="pattern"),
    )
    before_fact = _corpus(tmp_path, before).outbound[before.path][0]
    after_fact = _corpus(tmp_path, after, target).outbound[after.path][0]

    assert before_fact.authored_line != after_fact.authored_line
    assert before_fact.identity == after_fact.identity


def test_reviewed_none_bootstrap_and_stale_cleanup_order(tmp_path: Path) -> None:
    page = _state(
        tmp_path,
        "Knowledge Base/Notes/Insights/page.md",
        _source(exomem_id=_ID_A, body="## Relations\n"),
    )
    empty = _corpus(tmp_path)
    singleton = _corpus(tmp_path, page)
    reviewed = semantic_contract.RelationReviewState(
        kind="reviewed_none",
        page_identity=page.identity,
        content_fingerprint=page.review_fingerprint,
        reason="No honest relation yet",
    )

    current = _evaluate(
        before=page,
        after=page,
        before_corpus=singleton,
        after_corpus=singleton,
        before_review=reviewed,
        after_review=reviewed,
    )
    automatic = _evaluate(
        before=None,
        after=page,
        before_corpus=empty,
        after_corpus=singleton,
        operation="create",
    )

    assert current.relation_disposition.kind == "reviewed_none"
    assert automatic.relation_disposition.kind == "bootstrap"
    assert automatic.relation_disposition.satisfied

    other = _state(
        tmp_path,
        "Knowledge Base/Notes/Patterns/other.md",
        _source(title="Other", page_type="pattern", exomem_id=_ID_B),
    )
    linked = _state(
        tmp_path,
        page.path,
        _source(exomem_id=_ID_A, body="## Relations\n- supports [[Other]]\n"),
        review_fingerprint="review-v2",
    )
    linked_corpus = _corpus(tmp_path, linked, other)
    stale_plus_relation = _evaluate(
        before=page,
        after=linked,
        before_corpus=singleton,
        after_corpus=linked_corpus,
        before_review=reviewed,
        after_review=reviewed,
    )
    assert stale_plus_relation.relation_disposition.kind == "qualifying_relation"
    assert "cleanup_stale_review" in stale_plus_relation.actions


@pytest.mark.parametrize("kind", ["reviewed_none", "bootstrap"])
def test_qualifying_relation_cleans_up_current_non_edge_review(
    tmp_path: Path,
    kind: str,
) -> None:
    target = _state(
        tmp_path,
        "Knowledge Base/Notes/Patterns/target.md",
        _source(title="Target", page_type="pattern"),
    )
    page = _state(
        tmp_path,
        "Knowledge Base/Notes/Insights/page.md",
        _source(
            exomem_id=_ID_A,
            body="## Relations\n- supports [[Target]]\n",
        ),
    )
    corpus = _corpus(tmp_path, page, target)
    review = semantic_contract.RelationReviewState(
        kind=kind,
        page_identity=page.identity,
        content_fingerprint=page.review_fingerprint,
        reason="Previously reviewed",
    )

    result = _evaluate(
        before=page,
        after=page,
        before_corpus=corpus,
        after_corpus=corpus,
        before_review=review,
        after_review=review,
    )

    assert result.relation_disposition.kind == "qualifying_relation"
    assert result.actions == ("cleanup_relation_review",)


def test_edit_cannot_synthesize_bootstrap_and_entity_expires_bound_bootstrap(
    tmp_path: Path,
) -> None:
    page = _state(
        tmp_path,
        "Knowledge Base/Notes/Insights/page.md",
        _source(exomem_id=_ID_A, body="## Relations\n"),
    )
    singleton = _corpus(tmp_path, page)
    edit = _evaluate(
        before=page,
        after=page,
        before_corpus=singleton,
        after_corpus=singleton,
    )
    assert edit.relation_disposition.kind == "missing"
    assert edit.should_block
    assert any(
        f.resolved_rule == ("relations", "*", "disposition") for f in edit.errors
    )

    bound = semantic_contract.RelationReviewState(
        kind="bootstrap",
        page_identity=page.identity,
        content_fingerprint=page.review_fingerprint,
    )
    entity = _state(
        tmp_path,
        "Knowledge Base/Entities/person.md",
        _source(title="Person", page_type="entity"),
    )
    with_entity = _corpus(tmp_path, page, entity)
    stale = _evaluate(
        before=page,
        after=page,
        before_corpus=singleton,
        after_corpus=with_entity,
        before_review=bound,
        after_review=bound,
    )
    assert stale.relation_disposition.kind == "stale"
    assert stale.should_block


def test_posthoc_create_never_synthesizes_bootstrap(tmp_path: Path) -> None:
    page = _state(
        tmp_path,
        "Knowledge Base/Notes/Insights/page.md",
        _source(exomem_id=_ID_A, body="## Relations\n"),
    )
    empty = _corpus(tmp_path)
    singleton = _corpus(tmp_path, page)

    result = _evaluate(
        before=None,
        after=page,
        before_corpus=empty,
        after_corpus=singleton,
        operation="create",
        mode="posthoc",
    )

    assert result.relation_disposition.kind == "missing"
    assert not result.relation_disposition.satisfied
    assert "record_bootstrap_review" not in result.actions
    assert not result.should_block


def test_grandfathering_is_key_subset_not_error_count_and_posthoc_never_blocks(
    tmp_path: Path,
) -> None:
    before = _state(
        tmp_path,
        "Knowledge Base/Notes/Insights/page.md",
        _source(body="## Relations\n- (none yet)\n"),
    )
    same = _state(
        tmp_path,
        before.path,
        _source(body="Shift.\n\n## Relations\n- (none yet)\n"),
    )
    different = _state(
        tmp_path,
        before.path,
        _source(body="- [bad/category] value\n## Relations\n"),
    )
    before_corpus = _corpus(tmp_path, before)
    same_corpus = _corpus(tmp_path, same)
    different_corpus = _corpus(tmp_path, different)

    retained = _evaluate(
        before=before,
        after=same,
        before_corpus=before_corpus,
        after_corpus=same_corpus,
        grandfathered=True,
    )
    raw = _evaluate(
        before=before,
        after=same,
        before_corpus=before_corpus,
        after_corpus=same_corpus,
    )
    worsening = _evaluate(
        before=before,
        after=different,
        before_corpus=before_corpus,
        after_corpus=different_corpus,
        grandfathered=True,
    )
    posthoc = semantic_contract.evaluate(
        before=before,
        after=different,
        operation="observe",
        mode="posthoc",
        before_contracts=_contracts(),
        after_contracts=_contracts(),
        before_corpus=before_corpus,
        after_corpus=different_corpus,
        grandfathered=True,
    )

    assert any(
        finding.code == "malformed_relation" for finding in retained.warnings
    )
    retained_syntax = next(
        finding
        for finding in retained.warnings
        if finding.code == "malformed_relation"
    )
    raw_syntax = next(
        finding for finding in raw.errors if finding.code == "malformed_relation"
    )
    assert retained_syntax.key == raw_syntax.key
    assert not retained.should_block
    assert worsening.should_block
    assert not posthoc.should_block
    assert {finding.key for finding in worsening.errors} == {
        finding.key for finding in posthoc.errors
    }


def test_evaluate_is_pure_over_supplied_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = _state(
        tmp_path,
        "Knowledge Base/Notes/Insights/page.md",
        _source(body="## Relations\n"),
    )
    corpus = _corpus(tmp_path, page)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("pure evaluator crossed an adapter boundary")

    monkeypatch.setattr(Path, "read_text", forbidden)
    monkeypatch.setattr(semantic_contract.semantic_units, "parse_semantic_units", forbidden)
    monkeypatch.setattr(semantic_contract.relation_registry, "load_registry", forbidden)

    result = _evaluate(
        before=page,
        after=page,
        before_corpus=corpus,
        after_corpus=corpus,
    )

    assert result.operation == "edit"


def test_registry_diagnostics_keep_structured_category_and_relation_identities(
    tmp_path: Path,
) -> None:
    language = semantic_language_registry.load_registry(
        proposal={
            "schema_version": 1,
            "categories": {
                "config": {
                    "description": "Configuration",
                    "aliases": ["configuration"],
                    "scope": {"projects": ["alpha"]},
                }
            },
            "kinds": {},
        }
    )
    source = semantic_contract.build_page_state(
        tmp_path,
        "Knowledge Base/Notes/Insights/page.md",
        _source(
            project="beta",
            body=(
                "- [configuration] Value\n\n"
                "## Finding\n"
                "- relations: custom.rel: [[Target]]\n\n"
                "Finding body.\n"
            ),
        ),
        relation_registry=relation_registry.core_registry(),
        language_registry=language,
        review_fingerprint="review-v1",
    )
    target = _state(
        tmp_path,
        "Knowledge Base/Notes/Patterns/target.md",
        _source(title="Target", page_type="pattern"),
    )
    corpus = _corpus(tmp_path, source, target)

    result = _evaluate(
        before=source,
        after=source,
        before_corpus=corpus,
        after_corpus=corpus,
    )

    category_scope = [
        finding for finding in result.findings if finding.code == "scope_violation"
    ]
    assert [finding.resolved_rule for finding in category_scope] == [
        ("categories", "config", "registry")
    ]
    relation_registry_findings = [
        finding
        for finding in result.findings
        if finding.code in {"unsupported_relation", "unregistered_relation"}
    ]
    assert len(relation_registry_findings) == 1
    assert relation_registry_findings[0].resolved_rule == (
        "relations",
        "custom.rel",
        "registry",
    )
    assert not any(
        finding.resolved_rule == ("relations", "relations", "registry")
        for finding in result.findings
    )


@pytest.mark.parametrize(
    ("raw_key", "definition", "finding_code"),
    [
        (
            "science.bad",
            {
                "parent": "not_a_core_relation",
                "description": "Invalid extension",
            },
            "invalid_parent",
        ),
        ("science.rejected", "not an object", "invalid_definition"),
        ("science.rejected.multi.dot", "not an object", "invalid_definition"),
        (
            "science.invalid.multi.dot",
            {
                "parent": "not_a_core_relation",
                "description": "Invalid extension key and parent",
            },
            "invalid_parent",
        ),
    ],
)
def test_global_relation_registry_finding_uses_exact_extension_identity(
    tmp_path: Path,
    raw_key: str,
    definition: object,
    finding_code: str,
) -> None:
    registry = relation_registry.load_registry(
        proposal={
            "schema_version": 1,
            "extensions": {raw_key: definition},
        }
    )
    page = _state(
        tmp_path,
        "Knowledge Base/Notes/Insights/page.md",
        _source(body="## Relations\n"),
    )
    corpus = semantic_contract.SemanticCorpusContext.from_states(
        tmp_path,
        (page,),
        registry=registry,
        identity_census=_identity_census(page),
    )

    result = _evaluate(
        before=page,
        after=page,
        before_corpus=corpus,
        after_corpus=corpus,
    )

    finding = next(item for item in result.findings if item.code == finding_code)
    assert finding.governed_element_identity == ("relations", raw_key)
    assert finding.resolved_rule == ("relations", raw_key, "registry")


@pytest.mark.parametrize(
    ("extensions", "expected"),
    [
        (
            {
                "science.bad": {
                    "parent": "supports",
                    "description": "Short relation",
                    "extra": True,
                },
                "science.bad.extra": "not an object",
            },
            {
                "unknown_field": "science.bad",
                "invalid_key": "science.bad.extra",
                "invalid_definition": "science.bad.extra",
            },
        ),
        (
            {
                "science.bad": {
                    "parent": "supports",
                    "description": "Short relation",
                    "extra.parent": True,
                },
                "science.bad.extra": {
                    "parent": "not_core",
                    "description": "Long relation",
                },
            },
            {
                "unknown_field": "science.bad",
                "invalid_parent": "science.bad.extra",
            },
        ),
    ],
)
def test_relation_registry_identity_is_exact_when_extension_paths_collide(
    tmp_path: Path,
    extensions: dict[str, object],
    expected: dict[str, str],
) -> None:
    registry = relation_registry.load_registry(
        proposal={"schema_version": 1, "extensions": extensions}
    )
    page = _state(
        tmp_path,
        "Knowledge Base/Notes/Insights/page.md",
        _source(body="## Relations\n"),
    )
    corpus = semantic_contract.SemanticCorpusContext.from_states(
        tmp_path,
        (page,),
        registry=registry,
        identity_census=_identity_census(page),
    )

    result = _evaluate(
        before=page,
        after=page,
        before_corpus=corpus,
        after_corpus=corpus,
    )

    findings = {finding.code: finding for finding in result.findings}
    for code, relation in expected.items():
        assert findings[code].governed_element_identity == ("relations", relation)
        assert findings[code].resolved_rule == ("relations", relation, "registry")


def test_root_relation_registry_findings_use_distinct_registry_level_identities(
    tmp_path: Path,
) -> None:
    registry = relation_registry.load_registry(
        proposal={
            "schema_version": 2,
            "extensions": ["not an object"],
            "alpha": True,
            "beta": True,
        }
    )
    page = _state(
        tmp_path,
        "Knowledge Base/Notes/Insights/page.md",
        _source(body="## Relations\n"),
    )
    corpus = semantic_contract.SemanticCorpusContext.from_states(
        tmp_path,
        (page,),
        registry=registry,
        identity_census=_identity_census(page),
    )

    result = _evaluate(
        before=page,
        after=page,
        before_corpus=corpus,
        after_corpus=corpus,
    )

    by_code_and_path = {
        (finding.code, finding.governed_element_identity[-1]): finding
        for finding in result.findings
    }
    expected = {
        ("invalid_version", "schema_version"),
        ("invalid_extensions", "extensions"),
        ("unknown_field", "alpha"),
        ("unknown_field", "beta"),
    }
    assert expected <= set(by_code_and_path)
    for key in expected:
        finding = by_code_and_path[key]
        assert finding.governed_element_identity == (
            "relations",
            "registry",
            key[1],
        )
        assert finding.resolved_rule == ("relations", "*", "registry")
    assert len({by_code_and_path[key].key for key in expected}) == len(expected)


def test_contract_finding_key_is_exact_public_triple() -> None:
    finding = semantic_contract.ContractFinding(
        code="CODE",
        severity="error",
        path="Knowledge Base/page.md",
        span=None,
        detail="detail",
        remediation="fix",
        governed_element_identity=("fields", "status"),
        resolved_rule=("fields", "status", "required"),
        contracts=("test",),
        provenance=(),
    )

    assert finding.key == (
        "CODE",
        ("fields", "status"),
        ("fields", "status", "required"),
    )
