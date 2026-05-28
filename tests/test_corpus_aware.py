"""Tests for corpus-aware writes (suggest_related + detect_duplicates).

Logic tests (path canon, self/already-linked exclusion, hub re-rank) monkeypatch
find() and run torch-free. Semantic tests build the real sidecar over the fixture
vault and exercise dedup + note()'s suggestion block; they import-skip without
torch and lift the suite-wide embeddings gate.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kb_mcp import corpus_aware, find as find_module


# ---------------- pure / torch-free logic ----------------


def test_canon_normalizes_equivalent_forms() -> None:
    a = corpus_aware._canon("Knowledge Base/Notes/Insights/x.md")
    b = corpus_aware._canon("Notes/Insights/x")
    c = corpus_aware._canon("Knowledge Base/Notes/Insights/x.md#a-heading")
    assert a == b == c == "notes/insights/x"


def _hit(path: str, *, gid: int = 0, vr: int | None = None, br: int | None = None):
    return find_module.Hit(
        path=path, type="insight", scope=None, title=path.rsplit("/", 1)[-1],
        updated="", excerpt="ex", bm25_rank=br, vector_rank=vr, graph_in_degree=gid,
    )


def test_suggest_related_excludes_self_and_already_linked(monkeypatch) -> None:
    fake = [
        _hit("Knowledge Base/Notes/Insights/self.md"),
        _hit("Knowledge Base/Notes/Insights/linked.md"),
        _hit("Knowledge Base/Notes/Insights/fresh1.md"),
        _hit("Knowledge Base/Notes/Insights/fresh2.md"),
    ]
    monkeypatch.setattr(find_module, "find", lambda *a, **k: fake)
    out = corpus_aware.suggest_related(
        Path("/unused"), title="t", body="b",
        self_path="Knowledge Base/Notes/Insights/self",
        existing_links={"Knowledge Base/Notes/Insights/linked"},
        limit=8,
    )
    paths = {corpus_aware._canon(s.path) for s in out}
    assert "notes/insights/self" not in paths      # never suggest itself
    assert "notes/insights/linked" not in paths     # already linked
    assert {"notes/insights/fresh1", "notes/insights/fresh2"} <= paths


def test_suggest_related_prefers_hubs(monkeypatch) -> None:
    # find ranks the leaf first; a strongly-connected hub sits just below it.
    # The hub bonus must lift the hub to the top.
    fake = [
        _hit("Knowledge Base/Notes/Insights/leaf.md", gid=0),
        _hit("Knowledge Base/Notes/Insights/hub.md", gid=100),
    ]
    monkeypatch.setattr(find_module, "find", lambda *a, **k: fake)
    out = corpus_aware.suggest_related(Path("/unused"), title="t", body="b", limit=8)
    assert corpus_aware._canon(out[0].path) == "notes/insights/hub"


def test_suggest_related_why_mentions_signals(monkeypatch) -> None:
    fake = [_hit("Knowledge Base/Notes/Insights/x.md", gid=5, vr=2)]
    monkeypatch.setattr(find_module, "find", lambda *a, **k: fake)
    out = corpus_aware.suggest_related(Path("/unused"), title="t", body="b")
    assert "semantic #2" in out[0].why
    assert "hub" in out[0].why  # gid >= 3


def test_detect_duplicates_noop_when_embeddings_disabled(vault: Path) -> None:
    # Runs under the suite-wide KB_MCP_DISABLE_EMBEDDINGS — must short-circuit to
    # [] without loading torch or touching a sidecar.
    assert corpus_aware.detect_duplicates(
        vault, title="anything", body="some body", types_filter=["insight"]
    ) == []


# ---------------- semantic (model-loading) ----------------

pytest.importorskip("sentence_transformers")
pytest.importorskip("torch")

from kb_mcp import embeddings, note as note_module  # noqa: E402

_INSIGHT = "Knowledge Base/Notes/Insights/progressive-disclosure-without-mode-fragmentation.md"


@pytest.fixture
def embeddings_enabled(monkeypatch):
    monkeypatch.delenv("KB_MCP_DISABLE_EMBEDDINGS", raising=False)
    embeddings._IMPORT_FAILED = False


def test_detect_duplicates_flags_near_identical(vault: Path, embeddings_enabled) -> None:
    embeddings.EmbeddingIndex(vault).rebuild_all()
    page = find_module._CACHE.get(vault / _INSIGHT, vault)
    assert page is not None
    # Feed an existing page's own content back as a "draft" → near-perfect cosine.
    dups = corpus_aware.detect_duplicates(
        vault, title=page.title, body=page.body, types_filter=["insight"]
    )
    match = next((d for d in dups if "progressive-disclosure" in d.path), None)
    assert match is not None, f"expected the twin insight flagged; got {dups}"
    assert match.cosine >= corpus_aware.DUP_THRESHOLD


def test_detect_duplicates_respects_type_filter(vault: Path, embeddings_enabled) -> None:
    embeddings.EmbeddingIndex(vault).rebuild_all()
    page = find_module._CACHE.get(vault / _INSIGHT, vault)
    # Filtering to a type the twin isn't → it must not be returned.
    dups = corpus_aware.detect_duplicates(
        vault, title=page.title, body=page.body, types_filter=["pattern"]
    )
    assert not any("progressive-disclosure" in d.path for d in dups)


def test_note_attaches_suggestions_for_near_twin(vault: Path, embeddings_enabled) -> None:
    embeddings.EmbeddingIndex(vault).rebuild_all()
    twin = find_module._CACHE.get(vault / _INSIGHT, vault)
    res = note_module.note(
        vault,
        content=twin.body,
        note_type="insight",
        title="Near twin of progressive disclosure",
        tags=["ux"],
    )
    d = res.as_dict()
    assert d["path"]
    # The original insight should surface as a related-link suggestion.
    assert d.get("suggestions"), "expected suggestions for a near-twin insight"
    sugg = {corpus_aware._canon(s["path"]) for s in d["suggestions"]}
    assert "notes/insights/progressive-disclosure-without-mode-fragmentation" in sugg
