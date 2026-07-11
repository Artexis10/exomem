"""Typed-graph candidate expansion in the find graph lane.

Exercises the sidecar-backed graph lane: typed neighbours surface and outrank
plain `links_to`, inbound edges expand, placeholders are excluded, and the
wikilink fallback stays byte-identical to the pre-change ordering when the
sidecar is unavailable.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import embeddings as embeddings_module
from exomem import epistemic_graph
from exomem import find as find_module


@pytest.fixture(autouse=True)
def _clear_find_caches():
    """Flush the process-global find caches this module's find() calls populate,
    so a typed-mode ranking never bleeds into an unrelated later test."""
    yield
    find_module.clear_cache()
    embeddings_module.clear_embedding_indexes()

KB = "Knowledge Base/Notes/Insights"
SEED = f"{KB}/chloroplast-note.md"
EXPERIMENT = f"{KB}/experiment-note.md"
GLOSSARY = f"{KB}/glossary-note.md"
RELATED = f"{KB}/related-note.md"

# Frozen PRE-CHANGE fused ordering, captured by running the wikilink lane
# directly on the vault below before the typed branch existed. The fallback
# path must reproduce this byte-for-byte.
FALLBACK_FUSED = [
    "Knowledge Base/Notes/Insights/chloroplast-note.md",
    "Knowledge Base/Notes/Insights/related-note.md",
    "Knowledge Base/Notes/Insights/glossary-note.md",
    "Knowledge Base/Notes/Insights/experiment-note.md",
]


def _w(vault: Path, rel: str, body: str) -> None:
    p = vault / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


def _build_vault(tmp_path: Path, monkeypatch) -> Path:
    vault = tmp_path / "vault"
    _w(
        vault,
        SEED,
        """\
---
type: insight
status: active
---
# Chloroplast Note

The chloroplast is where photosynthesis happens in the plant cell.
photosynthesis photosynthesis.

See also [[Knowledge Base/Notes/Insights/related-note]].

## Relations

- evidenced_by [[Knowledge Base/Notes/Insights/experiment-note]]
- links_to [[Knowledge Base/Notes/Insights/glossary-note]]
- supports [[Knowledge Base/Notes/Insights/does-not-exist]]
""",
    )
    _w(vault, EXPERIMENT, "---\ntype: evidence\n---\n# Experiment Note\n\nA controlled trial with sealed jars and light meters.\n")
    _w(vault, GLOSSARY, "---\ntype: insight\n---\n# Glossary Note\n\nDefinitions of cellular structures and terms.\n")
    _w(vault, RELATED, "---\ntype: insight\n---\n# Related Note\n\nAdjacent botanical topics worth a glance.\n")
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    find_module.clear_cache()
    embeddings_module.clear_embedding_indexes()
    return vault


def _paths(vault: Path, **kw) -> list[str]:
    return [h.path for h in find_module.find(vault, query="photosynthesis", limit=15, graph=True, **kw)]


def _lane_paths(vault: Path, **kw) -> list[str]:
    """Fused order with the orthogonal type/status post-RRF multipliers off, so
    the graph lane's family precedence is the only reordering signal."""
    return _paths(vault, prefer_compiled=False, prefer_active=False, **kw)


def test_typed_neighbour_surfaces_for_conceptual_query(tmp_path, monkeypatch) -> None:
    vault = _build_vault(tmp_path, monkeypatch)
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    paths = _paths(vault)
    # experiment-note matches the query on no token; it is reachable only through
    # the typed `evidenced_by` edge, and must still enter results.
    assert EXPERIMENT in paths


def test_family_precedence_orders_typed_ahead_of_links_to(tmp_path, monkeypatch) -> None:
    vault = _build_vault(tmp_path, monkeypatch)
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    paths = _lane_paths(vault)
    assert EXPERIMENT in paths and GLOSSARY in paths and RELATED in paths
    # evidenced_by (evidence family) precedes plain links_to neighbours.
    assert paths.index(EXPERIMENT) < paths.index(GLOSSARY)
    assert paths.index(EXPERIMENT) < paths.index(RELATED)


def test_placeholder_target_excluded(tmp_path, monkeypatch) -> None:
    vault = _build_vault(tmp_path, monkeypatch)
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    paths = _paths(vault)
    assert not any("does-not-exist" in p for p in paths)


def test_inbound_edge_expands(tmp_path, monkeypatch) -> None:
    vault = tmp_path / "vault"
    seed = f"{KB}/target-seed.md"
    source = f"{KB}/source-note.md"
    _w(vault, seed, "---\ntype: insight\n---\n# Target Seed\n\nA note about quantum decoherence and quantum states.\n")
    _w(
        vault,
        source,
        """\
---
type: insight
---
# Source Note

An unrelated write-up on kitchen chemistry.

## Relations

- supports [[Knowledge Base/Notes/Insights/target-seed]]
""",
    )
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    find_module.clear_cache()
    embeddings_module.clear_embedding_indexes()
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    paths = [h.path for h in find_module.find(vault, query="quantum", limit=15, graph=True)]
    # source-note is the SOURCE of a typed edge whose destination is the seed;
    # the inbound edge makes it eligible for graph-lane expansion.
    assert source in paths


def test_fallback_equivalence_when_sidecar_disabled(tmp_path, monkeypatch) -> None:
    vault = _build_vault(tmp_path, monkeypatch)
    monkeypatch.setenv("EXOMEM_DISABLE_GRAPH_INDEX", "1")
    find_module.clear_cache()
    assert _paths(vault) == FALLBACK_FUSED


def _hits(vault, **kw):
    return find_module.find(vault, query="photosynthesis", limit=15, graph=True, **kw)


def test_annotated_typed_hit_carries_the_triple(tmp_path, monkeypatch) -> None:
    vault = _build_vault(tmp_path, monkeypatch)
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    hits = _hits(vault)
    experiment = next(h for h in hits if h.path == EXPERIMENT)
    graph = experiment.as_dict()["graph"]
    assert graph == {
        "relation_type": "evidenced_by",
        "direction": "outbound",
        "seed": SEED,
    }


def test_non_graph_hit_has_no_annotation(tmp_path, monkeypatch) -> None:
    vault = _build_vault(tmp_path, monkeypatch)
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    hits = _hits(vault)
    # The seed itself entered via BM25/keyword, not graph expansion.
    seed_hit = next(h for h in hits if h.path == SEED)
    assert "graph" not in seed_hit.as_dict()


def test_fallback_mode_never_annotates(tmp_path, monkeypatch) -> None:
    vault = _build_vault(tmp_path, monkeypatch)
    monkeypatch.setenv("EXOMEM_DISABLE_GRAPH_INDEX", "1")
    find_module.clear_cache()
    hits = _hits(vault)
    assert all("graph" not in h.as_dict() for h in hits)
