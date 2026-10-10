"""Recall admission filters every lane before fusion; it never sizes the pool."""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import find as find_module
from exomem import lexstore
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


def _recall(vault: Path, admit_path) -> list[dict]:
    trace = RetrievalTrace(
        requested_mode="hybrid",
        requested_result_level="page",
        rerank_requested=False,
        auto_rerank=False,
    )
    hits = find_module.find(
        vault,
        query=QUERY,
        limit=10,
        mode="hybrid",
        rerank=False,
        admit_path=admit_path,
        retrieval_trace=trace,
    )
    served = [hit.as_compact_dict() for hit in hits]
    attach_hit_explanations(trace, served)
    return served


def test_a_restricted_recall_reads_as_if_hidden_pages_crowding_every_lane_were_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Paths, lane ranks, lane scores and fused scores are the ones a vault
    without the hidden pages gives. Admission no longer deepens the pool to the
    admitted corpus, so this holds only while each lane ranks admitted pages
    alone, its BM25 statistics included."""
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
