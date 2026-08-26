"""Target-bound graph replacements derived from semantic write preflight state."""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import (
    relation_registry,
    semantic_contract,
    semantic_language_registry,
    vault,
)
from exomem.governance import catalog_publication, graph_producer, projected_graph


def _source(
    title: str,
    identity: str,
    *,
    body: str = "## Observations\n\n- [decision] Keep the graph exact.\n",
) -> str:
    return (
        "---\n"
        f"title: {title}\n"
        "type: insight\n"
        "status: active\n"
        "project: atlas\n"
        f"exomem_id: {identity}\n"
        "---\n\n"
        f"{body}"
    )


def _state(
    root: Path,
    path: str,
    source: str,
) -> semantic_contract.SemanticPageState:
    return semantic_contract.build_page_state(
        root,
        path,
        source,
        relation_registry=relation_registry.core_registry(),
        language_registry=semantic_language_registry.core_registry(),
        review_fingerprint="review-v1",
    )


def _corpus(
    root: Path,
    *states: semantic_contract.SemanticPageState,
) -> semantic_contract.SemanticCorpusContext:
    return semantic_contract.SemanticCorpusContext.from_states(
        root,
        states,
        registry=relation_registry.core_registry(),
        identity_census=semantic_contract.StableIdentityCensus(
            tuple(
                semantic_contract.StableIdentityEntry(state.path, state.identity)
                for state in states
            )
        ),
    )


def _write(
    root: Path,
    path: str,
    before: str,
    after: str,
) -> vault.PlannedWrite:
    return vault.PlannedWrite(
        root / path,
        after,
        expected_hash=vault.content_hash(before),
    )


def _edge(
    source: str,
    target: str,
    relation: str = "links_to",
) -> projected_graph.ProjectionGraphEdge:
    return projected_graph.ProjectionGraphEdge(source, target, relation)


def test_planned_source_replaces_its_exact_outgoing_edges(tmp_path: Path) -> None:
    source_path = "Knowledge Base/Notes/Insights/source.md"
    first_target = "Knowledge Base/Notes/Insights/first.md"
    second_target = "Knowledge Base/Notes/Insights/second.md"
    source_before = _source(
        "Source",
        "00000000-0000-0000-0000-000000000001",
        body="## Observations\n\n- [decision] First edge.\n\n[[First]]\n",
    )
    source_after = _source(
        "Source",
        "00000000-0000-0000-0000-000000000001",
        body="## Observations\n\n- [decision] Second edge.\n\n[[Second]]\n",
    )
    before = _corpus(
        tmp_path,
        _state(tmp_path, source_path, source_before),
        _state(
            tmp_path,
            first_target,
            _source("First", "00000000-0000-0000-0000-000000000002"),
        ),
        _state(
            tmp_path,
            second_target,
            _source("Second", "00000000-0000-0000-0000-000000000003"),
        ),
    )

    replacements = graph_producer.replacements_for_planned_markdown(
        tmp_path,
        before_corpus=before,
        writes=(_write(tmp_path, source_path, source_before, source_after),),
        semantic_states={source_path: _state(tmp_path, source_path, source_after)},
        language_registry=semantic_language_registry.core_registry(),
    )

    assert replacements == (
        catalog_publication.GraphMeasurementReplacement(
            source_path,
            vault.content_hash(source_after),
            (_edge(source_path, second_target),),
        ),
    )


def test_current_source_rebinds_its_exact_recovery_edges(tmp_path: Path) -> None:
    source_path = "Knowledge Base/Notes/Insights/source.md"
    target_path = "Knowledge Base/Notes/Insights/target.md"
    source = _source(
        "Source",
        "00000000-0000-0000-0000-000000000001",
        body="## Observations\n\n- [decision] Keep the edge.\n\n[[Target]]\n",
    )
    current = _corpus(
        tmp_path,
        _state(tmp_path, source_path, source),
        _state(
            tmp_path,
            target_path,
            _source("Target", "00000000-0000-0000-0000-000000000002"),
        ),
    )

    replacements = graph_producer.replacements_for_current_markdown(
        tmp_path,
        current_corpus=current,
        paths=(source_path,),
    )

    assert replacements == (
        catalog_publication.GraphMeasurementReplacement(
            source_path,
            vault.content_hash(source),
            (_edge(source_path, target_path),),
        ),
    )


def test_title_change_replaces_sources_whose_resolution_changed(tmp_path: Path) -> None:
    source_path = "Knowledge Base/Notes/Insights/source.md"
    target_path = "Knowledge Base/Notes/Insights/target.md"
    source = _source(
        "Source",
        "00000000-0000-0000-0000-000000000001",
        body=(
            "## Observations\n\n"
            "- [decision] Resolve the target by title.\n\n"
            "[[Old target title]]\n"
        ),
    )
    target_before = _source(
        "Old target title",
        "00000000-0000-0000-0000-000000000002",
    )
    target_after = _source(
        "New target title",
        "00000000-0000-0000-0000-000000000002",
    )
    before = _corpus(
        tmp_path,
        _state(tmp_path, source_path, source),
        _state(tmp_path, target_path, target_before),
    )

    replacements = graph_producer.replacements_for_planned_markdown(
        tmp_path,
        before_corpus=before,
        writes=(_write(tmp_path, target_path, target_before, target_after),),
        semantic_states={target_path: _state(tmp_path, target_path, target_after)},
        language_registry=semantic_language_registry.core_registry(),
    )

    assert replacements == (
        catalog_publication.GraphMeasurementReplacement(
            source_path,
            vault.content_hash(source),
            (),
        ),
        catalog_publication.GraphMeasurementReplacement(
            target_path,
            vault.content_hash(target_after),
            (),
        ),
    )


def test_reverse_supersession_replaces_the_logical_source_row(tmp_path: Path) -> None:
    old_path = "Knowledge Base/Notes/Insights/old.md"
    new_path = "Knowledge Base/Notes/Insights/new.md"
    old_before = _source(
        "Old",
        "00000000-0000-0000-0000-000000000001",
    )
    old_after = _source(
        "Old",
        "00000000-0000-0000-0000-000000000001",
        body="---\n",
    ).replace("status: active", "status: superseded").replace(
        "---\n\n---\n", "superseded_by: '[[New]]'\n---\n\n"
    )
    new_source = _source(
        "New",
        "00000000-0000-0000-0000-000000000002",
    )
    before = _corpus(
        tmp_path,
        _state(tmp_path, old_path, old_before),
        _state(tmp_path, new_path, new_source),
    )

    replacements = graph_producer.replacements_for_planned_markdown(
        tmp_path,
        before_corpus=before,
        writes=(_write(tmp_path, old_path, old_before, old_after),),
        semantic_states={old_path: _state(tmp_path, old_path, old_after)},
        language_registry=semantic_language_registry.core_registry(),
    )

    assert replacements == (
        catalog_publication.GraphMeasurementReplacement(
            new_path,
            vault.content_hash(new_source),
            (_edge(new_path, old_path, "supersedes"),),
        ),
        catalog_publication.GraphMeasurementReplacement(
            old_path,
            vault.content_hash(old_after),
            (),
        ),
    )


def test_catalog_bridge_forwards_lazy_graph_replacement_provider(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from exomem import semantic_writes

    replacement = catalog_publication.GraphMeasurementReplacement(
        "Knowledge Base/Notes/Insights/source.md",
        "a" * 64,
        (),
    )
    captured: dict[str, object] = {}

    def provider():
        return (replacement,)

    def fake_prepare(vault_root, *, writes, graph_replacement_provider):
        captured.update(
            vault_root=vault_root,
            writes=writes,
            graph_replacement_provider=graph_replacement_provider,
        )
        return "prepared"

    monkeypatch.setattr(catalog_publication, "prepare_planned_markdown_batch", fake_prepare)

    assert (
        semantic_writes._prepare_markdown_catalog_publication(
            tmp_path,
            (),
            graph_replacement_provider=provider,
        )
        == "prepared"
    )
    assert captured["graph_replacement_provider"] is provider


def test_media_sidecar_writer_forwards_lazy_graph_replacement_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import preserve

    sidecar = tmp_path / "Knowledge Base/Evidence/private.bin.md"
    write = vault.PlannedWrite(sidecar, "---\ntitle: Private\n---\n")
    captured: dict[str, object] = {}

    def fake_prepare(vault_root, *, writes, graph_replacement_provider):
        captured.update(
            vault_root=vault_root,
            writes=writes,
            graph_replacement_provider=graph_replacement_provider,
        )
        return None

    monkeypatch.setattr(catalog_publication, "prepare_planned_markdown_batch", fake_prepare)

    assert preserve.commit_media_sidecar_writes(
        tmp_path,
        (write,),
        batch_writer=lambda *_args, **_kwargs: [sidecar],
    ) == [sidecar]
    assert captured["writes"] == (write,)
    assert callable(captured["graph_replacement_provider"])


def test_creation_catalog_bridge_builds_provider_from_retained_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import semantic_writes

    source_path = "Knowledge Base/Notes/Insights/source.md"
    created_path = "Knowledge Base/Notes/Insights/created.md"
    source = _source(
        "Source",
        "00000000-0000-0000-0000-000000000001",
        body=(
            "## Observations\n\n"
            "- [decision] Resolve the future title.\n\n"
            "[[Created]]\n"
        ),
    )
    created = _source(
        "Created",
        "00000000-0000-0000-0000-000000000002",
    )
    created_state = _state(tmp_path, created_path, created)
    preflight = semantic_writes.CreationPreflight(
        applicability="full",
        destination=created_path,
        source=created,
        draft_id="draft-1",
        draft_token="token-1",
        mutated=False,
        contract_result=None,
        creation_validation=None,
        semantic_state=created_state,
        corpus=_corpus(tmp_path, _state(tmp_path, source_path, source)),
    )
    writes = (
        vault.PlannedWrite(
            tmp_path / created_path,
            created,
            create_only=True,
        ),
    )
    captured: dict[str, object] = {}

    def fake_prepare(
        vault_root,
        *,
        writes,
        graph_replacement_provider,
    ):
        captured.update(
            vault_root=vault_root,
            writes=writes,
            graph_replacement_provider=graph_replacement_provider,
        )
        return "prepared"

    monkeypatch.setattr(
        semantic_writes,
        "_prepare_markdown_catalog_publication",
        fake_prepare,
    )

    assert (
        semantic_writes._prepare_creation_catalog_publication(
            tmp_path,
            destination=preflight.destination,
            before_corpus=preflight.corpus,
            semantic_state=preflight.semantic_state,
            writes=writes,
        )
        == "prepared"
    )
    provider = captured["graph_replacement_provider"]
    assert callable(provider)
    assert provider() == (
        catalog_publication.GraphMeasurementReplacement(
            created_path,
            vault.content_hash(created),
            (),
        ),
        catalog_publication.GraphMeasurementReplacement(
            source_path,
            vault.content_hash(source),
            (_edge(source_path, created_path),),
        ),
    )


def test_semantic_transition_replaces_moved_target_and_its_referrers(
    tmp_path: Path,
) -> None:
    source_path = "Knowledge Base/Notes/Insights/source.md"
    old_path = "Knowledge Base/Notes/Insights/old.md"
    new_path = "Knowledge Base/Notes/Insights/new.md"
    source = _source(
        "Source",
        "00000000-0000-0000-0000-000000000001",
        body="## Observations\n\n- [decision] Follow the move.\n\n[[Target]]\n",
    )
    target = _source(
        "Target",
        "00000000-0000-0000-0000-000000000002",
    )
    before = _corpus(
        tmp_path,
        _state(tmp_path, source_path, source),
        _state(tmp_path, old_path, target),
    )
    moved_state = _state(tmp_path, new_path, target)
    after = _corpus(
        tmp_path,
        _state(tmp_path, source_path, source),
        moved_state,
    )
    writes = (
        vault.PlannedWrite(
            tmp_path / new_path,
            target,
            create_only=True,
        ),
    )

    replacements = graph_producer.replacements_for_semantic_transition(
        tmp_path,
        before_corpus=before,
        after_corpus=after,
        writes=writes,
        semantic_states={new_path: moved_state},
    )

    assert replacements == (
        catalog_publication.GraphMeasurementReplacement(
            new_path,
            vault.content_hash(target),
            (),
        ),
        catalog_publication.GraphMeasurementReplacement(
            source_path,
            vault.content_hash(source),
            (_edge(source_path, new_path),),
        ),
    )
