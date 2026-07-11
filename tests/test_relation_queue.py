"""Deterministic, read-only relation-acceptance queue assembly and filtering."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from exomem import relation_queue


def _write_page(
    vault: Path,
    name: str,
    body: str,
    *,
    page_type: str = "insight",
    status: str = "active",
    folder: str = "Notes/Insights",
) -> Path:
    path = vault / "Knowledge Base" / folder / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\ntype: {page_type}\nstatus: {status}\n---\n# {name}\n\n{body}\n",
        encoding="utf-8",
    )
    return path


def _seed(vault: Path) -> None:
    # alpha -> beta: a live wikilink to an existing page (SHOWN).
    _write_page(vault, "alpha", "See [[Knowledge Base/Notes/Insights/beta]].")
    # beta: exists, no outbound candidates.
    _write_page(vault, "beta", "A measured fact.")
    # gamma -> beta already authored under ## Relations (authored-edge FILTERED).
    _write_page(
        vault,
        "gamma",
        "## Relations\n\n- links_to [[Knowledge Base/Notes/Insights/beta]]\n\n"
        "Also see [[Knowledge Base/Notes/Insights/beta]].",
    )
    # delta -> ghost-source: a frontmatter `sources:` edge whose target file
    # does not exist (placeholder-target FILTERED). Frontmatter-source
    # candidates do not existence-check at suggestion time, so this reaches the
    # queue's own placeholder filter.
    delta = vault / "Knowledge Base" / "Notes" / "Insights" / "delta.md"
    delta.parent.mkdir(parents=True, exist_ok=True)
    delta.write_text(
        "---\ntype: insight\nstatus: active\n"
        "sources:\n  - Knowledge Base/Sources/ghost-source.md\n---\n"
        "# delta\n\nA claim with a dangling source.\n",
        encoding="utf-8",
    )


def _tree_hash(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file():
            h.update(p.relative_to(root).as_posix().encode("utf-8"))
            h.update(b"\0")
            h.update(p.read_bytes())
            h.update(b"\0")
    return h.hexdigest()


def _all_items(result: dict) -> list[dict]:
    return [item for group in result["groups"] for item in group["items"]]


def test_queue_is_deterministic_on_unchanged_corpus(tmp_path: Path) -> None:
    _seed(tmp_path)
    first = relation_queue.build_queue(tmp_path)
    second = relation_queue.build_queue(tmp_path)
    assert first == second


def test_queue_shows_live_edge_and_reports_propose_only(tmp_path: Path) -> None:
    _seed(tmp_path)
    result = relation_queue.build_queue(tmp_path)
    assert result["mutated"] is False
    items = _all_items(result)
    edges = {(it["from"], it["to"], it["relation_type"]) for it in items}
    assert (
        "Knowledge Base/Notes/Insights/alpha.md",
        "Knowledge Base/Notes/Insights/beta.md",
        "links_to",
    ) in edges


def test_authored_edge_is_filtered(tmp_path: Path) -> None:
    _seed(tmp_path)
    result = relation_queue.build_queue(tmp_path)
    items = _all_items(result)
    gamma_beta = [
        it
        for it in items
        if it["from"].endswith("gamma.md") and it["to"].endswith("beta.md")
    ]
    assert gamma_beta == []
    assert result["filtered"]["authored_edge"] >= 1


def test_placeholder_target_is_filtered(tmp_path: Path) -> None:
    _seed(tmp_path)
    result = relation_queue.build_queue(tmp_path)
    items = _all_items(result)
    assert not any(it["to"].endswith("ghost.md") for it in items)
    assert result["filtered"]["placeholder_target"] >= 1


def test_dismissed_candidate_is_filtered(tmp_path: Path) -> None:
    _seed(tmp_path)
    result = relation_queue.build_queue(tmp_path)
    item = next(
        it
        for it in _all_items(result)
        if it["from"].endswith("alpha.md") and it["to"].endswith("beta.md")
    )
    relation_queue.triage(tmp_path, ref=item["ref"], action="dismiss")
    after = relation_queue.build_queue(tmp_path)
    assert item["ref"] not in {it["ref"] for it in _all_items(after)}
    assert after["filtered"]["decided"] >= 1


def test_coverage_counters_align_with_activation(tmp_path: Path) -> None:
    _seed(tmp_path)
    result = relation_queue.build_queue(tmp_path)
    coverage = result["coverage"]
    assert coverage["eligible_pages"] >= 4
    assert "relation_candidate_pages_found" in coverage
    assert result["shown"] == len(_all_items(result))
    # The full (uncapped) corpus was scanned this call — nothing is partial.
    assert coverage["relation_scan_complete"] is True
    assert result["pages_truncated"] is False


def test_cap_stops_candidate_generation_early_and_reports_honestly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Several candidate-bearing eligible pages, but cap at one group. Bug:
    # candidate generation (suggest_relations, which can invoke embedding
    # scoring) ran for EVERY eligible page before slicing the result down to
    # the cap — unacceptable on a large vault. The fix must stop generating
    # once `limit_pages` groups with open items are collected.
    _write_page(tmp_path, "page-a", "See [[Knowledge Base/Notes/Insights/page-z]].")
    _write_page(tmp_path, "page-b", "See [[Knowledge Base/Notes/Insights/page-z]].")
    _write_page(tmp_path, "page-c", "See [[Knowledge Base/Notes/Insights/page-z]].")
    _write_page(tmp_path, "page-z", "See [[Knowledge Base/Notes/Insights/page-a]].")

    calls: list[str] = []
    real_page_candidates = relation_queue._page_candidates

    def counting_page_candidates(vault_root, page, *, limit_per_page):
        calls.append(page.rel_path)
        return real_page_candidates(vault_root, page, limit_per_page=limit_per_page)

    monkeypatch.setattr(relation_queue, "_page_candidates", counting_page_candidates)

    capped = relation_queue.build_queue(tmp_path, limit_pages=1)

    assert capped["pages_shown"] == 1
    # The defect: this call would equal 4 (every eligible page) before the fix.
    assert len(calls) < 4, f"candidate generation ran for {calls}, not just the capped prefix"
    assert capped["pages_scanned"] == len(calls)
    # Honest capped-surfacing signal: we stopped before the corpus was fully
    # scanned, so totals beyond the shown prefix are explicitly NOT claimed.
    assert capped["pages_truncated"] is True
    assert capped["pages_unscanned"] >= 1
    assert capped["coverage"]["relation_scan_complete"] is False


def test_read_never_writes_to_the_vault(tmp_path: Path) -> None:
    _seed(tmp_path)
    before = _tree_hash(tmp_path)
    relation_queue.build_queue(tmp_path)
    assert _tree_hash(tmp_path) == before
