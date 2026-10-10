"""Recall admission narrows what each lane ranks before fusion; it never sizes the pool.

Embeddings are off, so these tests cover only the BM25, keyword and graph lanes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import epistemic_graph, lexstore
from exomem import find as find_module
from exomem.retrieval_explain import RetrievalTrace, attach_hit_explanations

QUERY = "orchard pruning"
VISIBLE = 60
#: Visible pages without the query terms. With them the terms occur in under
#: half the admitted corpus, so their BM25 weight depends on which pages the
#: statistics count (a term in most pages gets FTS5's floor weight everywhere).
UNRELATED = 130
#: More hidden pages than either lexical lane holds at limit 10 (BM25 50,
#: keyword 150). Each one is a stronger and more recent match than any visible
#: page, so a lane that ranked first and admitted afterwards would hand fusion
#: hidden pages only.
HIDDEN = 160


def _note(root: Path, name: str, *, body: str, updated: str) -> str:
    rel = f"Knowledge Base/Notes/{name}.md"
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\ntype: insight\ntitle: {name}\nupdated: {updated}\n---\n\n# {name}\n\n{body}\n",
        encoding="utf-8",
    )
    return rel


def _vault(root: Path, *, hidden_pages: bool) -> set[str]:
    for i in range(VISIBLE):
        _note(
            root,
            f"visible-{i:03d}",
            body=f"Orchard note {i} on pruning, quince variety {i % 7}.",
            updated=f"2024-01-{1 + i % 28:02d}",
        )
    for i in range(UNRELATED):
        _note(
            root,
            f"unrelated-{i:03d}",
            body=f"Tide table {i} for the harbour.",
            updated="2023-03-01",
        )
    hidden = {
        _note(
            root,
            f"hidden-{i:03d}",
            body="Orchard orchard orchard pruning orchard.",
            updated="2025-06-01",
        )
        for i in range(HIDDEN if hidden_pages else 0)
    }
    lexstore.ensure_fresh(root)
    return hidden


def _recall(vault: Path, admit_path, *, query: str = QUERY, scope: str = "kb") -> list[dict]:
    trace = RetrievalTrace(
        requested_mode="hybrid",
        requested_result_level="page",
        rerank_requested=False,
        auto_rerank=False,
    )
    hits = find_module.find(
        vault,
        query=query,
        limit=10,
        scope=scope,
        mode="hybrid",
        rerank=False,
        admit_path=admit_path,
        retrieval_trace=trace,
    )
    served = [hit.as_dict() for hit in hits]
    attach_hit_explanations(trace, served)
    return served


def test_a_restricted_recall_reads_as_if_hidden_pages_crowding_both_lexical_lanes_were_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Paths, lane ranks, BM25 scores and fused scores are the ones a vault
    without the hidden pages gives. Admission no longer deepens the pool to the
    admitted corpus, so this holds only while the BM25 and keyword lanes rank
    admitted pages alone, BM25's corpus statistics included."""
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_CLIP", "1")
    present, absent = tmp_path / "present", tmp_path / "absent"
    hidden = _vault(present, hidden_pages=True)
    _vault(absent, hidden_pages=False)

    def admit(path: str) -> bool:
        return path not in hidden

    owner = _recall(present, None)
    assert {hit["path"] for hit in owner} <= hidden, owner

    restricted = _recall(present, admit)
    assert len(restricted) == 10
    assert restricted == _recall(absent, admit)


def _without_bm25_raw_scores(hits: list[dict]) -> list[dict]:
    """The admitted BM25 ranking scores in Python and the unrestricted one in
    FTS5, so their raw scores differ in the last digits while ranks agree."""
    for hit in hits:
        hit["ranking_explanation"]["lanes"].get("bm25", {}).pop("raw_score", None)
    return hits


def test_admitting_every_page_recalls_what_an_unrestricted_caller_recalls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Admission never deepens the candidate pool. "orchard" matches 220 pages
    here, every one admitted, and the caller asks for 10. A pool sized by the
    admitted corpus fused all of them, so deep BM25 ranks reached the served
    list and the post-fusion multipliers re-ordered a deeper pool."""
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_CLIP", "1")
    _vault(tmp_path, hidden_pages=True)

    unrestricted = _recall(tmp_path, None, query="orchard")
    admit_all = _recall(tmp_path, lambda _path: True, query="orchard")

    assert len(unrestricted) == 10
    assert _without_bm25_raw_scores(admit_all) == _without_bm25_raw_scores(unrestricted)


LINKED = "Knowledge Base/Notes/Shared Name.md"


def _namesake_vault(root: Path, *, linker: str, hidden_namesakes: bool, sidecar: bool) -> None:
    """A visible page links `[[Shared Name]]` and `[[Shared Hub]]`, each the
    stem of one visible page. The hidden pages, when present, share both stems.
    "Shared Hub" matches the query, so its in-degree is a served signal."""
    pages = {
        linker: "Quince grafting on dwarf rootstock. See [[Shared Name]] and [[Shared Hub]].\n\n"
        "## Relations\n\n- supports [[Shared Name]]\n",
        LINKED: "Catalogue of rootstock entries.\n",
        "Knowledge Base/Notes/Shared Hub.md": "Quince grafting hub.\n",
        **{f"Knowledge Base/Notes/quince-{i}.md": f"Quince grafting note {i}.\n" for i in range(4)},
    }
    if hidden_namesakes:
        pages["Knowledge Base/Private/Shared Name.md"] = "Private ledger.\n"
        pages["Knowledge Base/Private/Shared Hub.md"] = "Private ledger.\n"
    for rel, body in pages.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"---\ntype: insight\n---\n\n# {path.stem}\n\n{body}", encoding="utf-8")
    lexstore.ensure_fresh(root)
    if sidecar:
        epistemic_graph.EpistemicGraphIndex(root).rebuild_all()


@pytest.mark.parametrize(
    ("linker", "scope", "sidecar"),
    [
        ("Knowledge Base/Notes/quince-linker.md", "kb", True),
        ("Knowledge Base/Notes/quince-linker.md", "kb", False),
        # The sidecar indexes only the KB, so this seed takes the legacy expansion.
        ("Reference/quince-linker.md", "vault", True),
    ],
    ids=["typed-sidecar", "wikilink-fallback", "out-of-kb-seed"],
)
def test_a_hidden_namesake_never_changes_how_a_restricted_callers_links_resolve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, linker: str, scope: str, sidecar: bool
) -> None:
    """The graph lane resolves a visible page's links over the caller's view.
    Over the whole vault a hidden namesake makes `[[Shared Name]]` ambiguous,
    so the visible target leaves the graph lane and `[[Shared Hub]]` stops
    counting toward a served hit's in-degree: both reveal the hidden page."""
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_CLIP", "1")
    present, absent = tmp_path / "present", tmp_path / "absent"
    _namesake_vault(present, linker=linker, hidden_namesakes=True, sidecar=sidecar)
    _namesake_vault(absent, linker=linker, hidden_namesakes=False, sidecar=sidecar)

    def admit(path: str) -> bool:
        return not path.startswith("Knowledge Base/Private/")

    owner = _recall(present, None, query="quince grafting", scope=scope)
    assert LINKED not in {hit["path"] for hit in owner}, owner
    without_namesake = _recall(absent, admit, query="quince grafting", scope=scope)
    assert LINKED in {hit["path"] for hit in without_namesake}, without_namesake

    assert _recall(present, admit, query="quince grafting", scope=scope) == without_namesake
