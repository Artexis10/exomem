"""Consolidation control and private staging are operational state, not knowledge."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from exomem import (
    attention,
    bm25,
    commands,
    embeddings,
    epistemic_graph,
    file_watcher,
    find_corpus,
    hosted_portability,
    index_paths,
)
from exomem import find as find_module
from exomem import vault as vault_module
from exomem.embedding_index import VECTOR_DIM, EmbeddingIndex

SENTINEL = "zz-consolidation-private-oracle-7d91"


def _seed_vault(vault: Path) -> None:
    notes = vault / "Knowledge Base" / "Notes"
    notes.mkdir(parents=True)
    (notes / "source.md").write_text(
        "---\ntype: insight\nstatus: active\n---\n"
        "# Source\n\nVisible knowledge.\n\n"
        "## Relations\n\n- supports [[Knowledge Base/Notes/target]]\n",
        encoding="utf-8",
    )
    (notes / "target.md").write_text(
        "---\ntype: insight\nstatus: active\n---\n# Target\n\nVisible target.\n",
        encoding="utf-8",
    )


def _portability_context() -> hosted_portability.PortabilityContext:
    return hosted_portability.PortabilityContext(
        cell_id="cell-isolation-7d91",
        vault_id="vault-isolation-7d91",
        operation_id="operation-isolation-7d91",
        created_at="2026-08-28T09:00:00+00:00",
        operator_authorized=True,
        lifecycle_state="quiesced",
        routing_stopped=True,
        active_mutations=0,
        background_writers_stopped=True,
        reads_allowed=True,
    )


def _normalize_graph_rows(rows: list[dict]) -> list[dict]:
    return [
        {
            key: value
            for key, value in row.items()
            if key not in {"metadata", "source_hash"}
        }
        for row in rows
    ]


def _snapshot(
    vault: Path,
    artifact_root: Path,
    *,
    monkeypatch: pytest.MonkeyPatch,
) -> dict:
    find_module.clear_cache()
    bm25.clear_cache()
    monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", "python")
    monkeypatch.setenv("EXOMEM_INDEX_SCOPE", "vault")
    monkeypatch.delenv("EXOMEM_DISABLE_GRAPH_INDEX", raising=False)

    def embed_texts(texts, **_kwargs):  # noqa: ANN001
        if not texts:
            return np.zeros((0, VECTOR_DIM), dtype=np.float32)
        return np.full(
            (len(texts), VECTOR_DIM),
            1.0 / np.sqrt(VECTOR_DIM),
            dtype=np.float32,
        )

    monkeypatch.setattr(embeddings, "embed_texts", embed_texts)

    keyword = {
        scope: bm25.BM25Index().search(vault, SENTINEL, 20, scope=scope)
        for scope in ("kb", "vault")
    }

    vector_index = EmbeddingIndex(vault)
    vector_index.rebuild_all()
    vector_metadata, _matrix = vector_index.all_vectors()

    graph = epistemic_graph.EpistemicGraphIndex(vault)
    graph.rebuild_all()

    exported = hosted_portability.export_quiesced_vault(
        vault,
        artifact_root,
        context=_portability_context(),
        exomem_release="9.9.9",
    )

    watcher = file_watcher.FileWatcher(vault)
    return {
        "keyword": keyword,
        "kb_walk": [
            path.relative_to(vault).as_posix()
            for path in sorted(find_corpus.walk_md(vault / "Knowledge Base"))
        ],
        "vault_walk": [
            path.relative_to(vault).as_posix()
            for path in sorted(vault_module.walk_vault_md(vault))
        ],
        "vector_paths": sorted({path for path, _chunk in vector_metadata}),
        "graph_nodes": _normalize_graph_rows(graph.nodes()),
        "graph_edges": _normalize_graph_rows(graph.edges()),
        "review": attention.attention(
            vault,
            categories=["relation_debt"],
            limit=25,
            record_surfacing=False,
        ).as_dict(),
        "overview": commands.op_browse_memory(
            vault,
            mode="overview",
            include_hidden=True,
        ),
        "resources": commands.op_browse_memory(
            vault,
            mode="list",
            recursive=True,
            include_hidden=True,
        ),
        "watcher_kb": sorted(watcher._walk_entries("kb")),
        "watcher_vault": sorted(watcher._walk_entries("vault")),
        "export_manifest": exported.manifest,
        "export_bytes": exported.archive_path.read_bytes(),
        "fingerprint": hosted_portability.canonical_vault_fingerprint(vault),
    }


def test_run_and_private_artifact_state_are_absent_from_every_knowledge_surface(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = tmp_path / "vault"
    _seed_vault(vault)
    before = _snapshot(vault, tmp_path / "export-before", monkeypatch=monkeypatch)

    run_file = (
        vault
        / "Knowledge Base"
        / "_Consolidation"
        / "runs"
        / "00000000-0000-4000-8000-000000000001"
        / "run.json"
    )
    run_file.parent.mkdir(parents=True)
    run_file.write_text(
        json.dumps(
            {
                "schema": "exomem.consolidation-run/v1",
                "phase": "planning",
                "private_probe": SENTINEL,
            }
        ),
        encoding="utf-8",
    )
    (run_file.parent / "conflicted-copy.md").write_text(
        f"# Private run conflict\n\n{SENTINEL}\n",
        encoding="utf-8",
    )
    private_artifact = tmp_path / "private-artifacts" / "objects" / "payload.md"
    private_artifact.parent.mkdir(parents=True)
    private_artifact.write_text(f"# Private staging\n\n{SENTINEL}\n", encoding="utf-8")

    after = _snapshot(vault, tmp_path / "export-after", monkeypatch=monkeypatch)

    assert after == before
    assert not any("_Consolidation" in path for path in after["vault_walk"])
    assert not any("_Consolidation" in path for path in after["vector_paths"])


def test_incremental_watcher_drops_consolidation_state_before_fanout(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    _seed_vault(vault)
    run_file = (
        vault
        / "Knowledge Base"
        / "_Consolidation"
        / "runs"
        / "00000000-0000-4000-8000-000000000001"
        / "private-run-state.md"
    )
    run_file.parent.mkdir(parents=True)
    run_file.write_text(f'{{"private_probe":"{SENTINEL}"}}\n', encoding="utf-8")
    watcher = file_watcher.FileWatcher(vault)

    watcher._record(run_file, deleted=False)
    watcher._record(run_file, deleted=True)

    assert vault_module.in_excluded_scan_dir(
        run_file.relative_to(vault).as_posix()
    )
    assert watcher._pending_upsert == set()
    assert watcher._pending_delete == set()
    assert watcher._pending_media == set()
    assert watcher._pending_external_epoch == 0


def test_vector_index_walk_is_the_reserved_full_vault_walk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = tmp_path / "vault"
    _seed_vault(vault)
    monkeypatch.setenv("EXOMEM_INDEX_SCOPE", "vault")
    run_file = vault / "Knowledge Base" / "_Consolidation" / "private.md"
    run_file.parent.mkdir(parents=True)
    run_file.write_text(SENTINEL, encoding="utf-8")

    assert list(index_paths.iter_index_markdown(vault)) == list(
        vault_module.walk_vault_md(vault)
    )
    assert run_file not in set(index_paths.iter_index_markdown(vault))
