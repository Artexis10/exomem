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
        tmp_path, (authored, older), registry=registry
    )
    extension, lifecycle = corpus.outbound[authored.path][0], corpus.outbound[older.path][0]

    assert semantic_contract.qualify_relation(extension, registry=registry, corpus=corpus).qualifies
    assert lifecycle.authored_path == authored.path
    assert lifecycle.logical_source_path == older.path
    assert lifecycle.logical_target_path == authored.path


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
        tmp_path, (source, target), registry=registry
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
