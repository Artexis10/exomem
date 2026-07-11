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


# Derived, rebuildable index sidecars that read paths may lazily (re)build.
# The purity contract is about knowledge state — Markdown, graph EDGES, and
# review-state — not derived index maintenance (e.g. .refs.sqlite from the
# reference-enrichment index, which any read op may create on first touch).
_DERIVED_INDEX_NAMES = {".refs.sqlite", ".embeddings.sqlite", ".clip.sqlite"}


def _is_derived_index(p: Path) -> bool:
    name = p.name
    return any(
        name == base or name.startswith(base + "-") for base in _DERIVED_INDEX_NAMES
    )


def _tree_hash(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file() and not _is_derived_index(p):
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


def test_dismissed_candidate_resurfaces_when_evidence_changes_without_source_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Some candidate methods (shared_sources, embedding_proximity) derive
    # evidence from ANOTHER page or the corpus index, not from the "from"
    # page's own content. If the fingerprint only folds in the source page's
    # signal_version (as review_state.fingerprint does whenever meta.signal_
    # version is supplied — it then ignores `detail` entirely), an evidence
    # change driven by something other than an edit to the source page keeps
    # the same fingerprint, and a stale dismissal never expires.
    _write_page(tmp_path, "alpha", "See [[Knowledge Base/Notes/Insights/beta]].")
    _write_page(tmp_path, "beta", "A measured fact.")
    evidence_holder = {"cosine": 0.42}
    real_page_candidates = relation_queue._page_candidates

    def fake_page_candidates(vault_root, page, *, limit_per_page):
        if not page.rel_path.endswith("alpha.md"):
            return real_page_candidates(vault_root, page, limit_per_page=limit_per_page)
        return [
            {
                "from": page.rel_path,
                "to": "Knowledge Base/Notes/Insights/beta.md",
                "relation_type": "relates_to",
                "method": "embedding_proximity",
                "evidence": dict(evidence_holder),
            }
        ]

    monkeypatch.setattr(relation_queue, "_page_candidates", fake_page_candidates)

    first = relation_queue.build_queue(tmp_path)
    item = next(it for it in _all_items(first) if it["from"].endswith("alpha.md"))
    relation_queue.triage(tmp_path, ref=item["ref"], action="dismiss")
    still_dismissed = relation_queue.build_queue(tmp_path)
    assert item["ref"] not in {it["ref"] for it in _all_items(still_dismissed)}

    # The evidence changes (e.g. corpus-wide embedding drift from an edit to
    # SOME OTHER page) — alpha.md itself is untouched on disk.
    evidence_holder["cosine"] = 0.91
    resurfaced = relation_queue.build_queue(tmp_path)
    resurfaced_item = next(
        it for it in _all_items(resurfaced) if it["from"].endswith("alpha.md")
    )
    assert resurfaced_item["fingerprint"] != item["fingerprint"]
    assert resurfaced_item["ref"] in {it["ref"] for it in _all_items(resurfaced)}


def test_accept_refuses_when_target_deleted_between_read_and_accept(
    tmp_path: Path,
) -> None:
    # A frontmatter-`sources` candidate does not existence-check its target at
    # suggestion time (that's the queue's OWN placeholder filter, applied at
    # read time). If the target is deleted AFTER the queue read but BEFORE
    # accept, the candidate's identity/evidence/fingerprint are all unchanged
    # (nothing about the SOURCE page or its evidence differs) — only accept's
    # own live eligibility re-check catches the now-dangling target.
    page = tmp_path / "Knowledge Base" / "Notes" / "Insights" / "epsilon.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(
        "---\ntype: insight\nstatus: active\n"
        "sources:\n  - Knowledge Base/Sources/ephemeral-source.md\n---\n"
        "# epsilon\n\nA claim backed by a source that will vanish.\n",
        encoding="utf-8",
    )
    source = tmp_path / "Knowledge Base" / "Sources" / "ephemeral-source.md"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        "---\ntype: source\nstatus: unprocessed\ningested_into: []\n---\n"
        "# Ephemeral source\n\nWill be deleted before accept.\n",
        encoding="utf-8",
    )

    result = relation_queue.build_queue(tmp_path)
    group = next(g for g in result["groups"] if g["path"].endswith("epsilon.md"))
    item = next(it for it in group["items"] if it["to"].endswith("ephemeral-source.md"))
    before = page.read_bytes()

    source.unlink()  # target deleted between read and accept

    def _unexpected_edit(*_args, **_kwargs):
        raise AssertionError("edit_memory must not be called when the candidate is stale")

    with pytest.raises(ValueError, match="REVIEW_ITEM_CHANGED"):
        relation_queue.accept(
            tmp_path,
            ref=item["ref"],
            expected_hash=group["content_hash"],
            why="Accepted reviewed relation",
            expected_fingerprint=item["fingerprint"],
            edit_memory=_unexpected_edit,
        )
    assert page.read_bytes() == before  # no bullet was appended


def test_read_never_writes_to_the_vault(tmp_path: Path) -> None:
    _seed(tmp_path)
    before = _tree_hash(tmp_path)
    relation_queue.build_queue(tmp_path)
    assert _tree_hash(tmp_path) == before
