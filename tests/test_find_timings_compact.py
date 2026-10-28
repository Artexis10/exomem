"""Opt-in `find` timing diagnostics + compact/full result detail.

Default behavior must stay byte-identical: no envelope, full hit dicts with
excerpts. Timings are opt-in via `include_timings`, compact via `detail`.
A compact hit keeps a short excerpt and drops the ranking signals.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import commands, lexstore, query_log
from exomem import find as find_module
from exomem.find_types import Hit
from exomem.governance import egress


def test_default_find_shape_unchanged(vault: Path) -> None:
    out = commands.op_find(vault, query="metabolism")
    assert isinstance(out, list)
    assert out, "fixture vault should match 'metabolism'"
    assert "excerpt" in out[0]
    assert "timings" not in out[0]


@pytest.mark.parametrize("hints_available", [True, False])
def test_timings_envelope_and_stages(
    vault: Path, warm_managed_cell, monkeypatch, hints_available: bool
) -> None:
    warm_managed_cell(vault)
    if not hints_available:
        monkeypatch.setattr(
            lexstore, "emitted_parent_hints_result",
            lambda *_args, **_kwargs: lexstore.CatalogQueryResult(
                None, lexstore.CatalogReadiness("stale", False, lexstore.backend())
            ),
        )
    out = commands.op_find(vault, query="metabolism", include_timings=True)
    assert isinstance(out, dict)
    assert set(out) == {"hits", "timings"}
    assert out["hits"]
    t = out["timings"]
    assert t["total_ms"] >= 0
    assert "cache" in t and "hit" in t["cache"]
    assert t["profile"]["mode"] == "hybrid"
    assert t["profile"]["detail"] == "full"
    assert t["profile"]["pack"] is False
    assert t["profile"]["auto_rerank"] is False
    assert t["profile"]["compute_policy"]["mode"] in {"quiet", "normal", "performance"}
    stages = t["stages"]
    # Lexical lanes always run in hybrid mode; vector is present as a timed
    # or errored stage even with embeddings disabled (soft-fail, never fatal).
    assert "bm25" in stages
    assert "keyword" in stages
    assert "vector" in stages
    assert "parent_hints" in stages
    assert stages["parent_hints"]["source"] == ("index" if hints_available else "declined")
    assert "fusion" in stages
    assert "filter_hits" in stages
    assert "serialize" in stages
    # No content leaks into diagnostics: numbers, flags, short names only.
    for entry in stages.values():
        for key in entry:
            assert key in ("ms", "skipped", "error", "calls", "cache_hit", "source", "parent")


def test_parent_hint_stage_is_absent_without_candidates(vault: Path) -> None:
    out = commands.op_find(
        vault, query="nomatchingqueryzz", include_timings=True
    )

    assert out["hits"] == []
    assert "parent_hints" not in out["timings"]["stages"]


def test_timed_ranking_matches_untimed(vault: Path) -> None:
    plain = commands.op_find(vault, query="metabolism")
    timed = commands.op_find(vault, query="metabolism", include_timings=True)
    assert [h["path"] for h in timed["hits"]] == [h["path"] for h in plain]


def test_timings_with_pack_envelope(vault: Path) -> None:
    out = commands.op_find(
        vault, query="metabolism", pack=True, include_timings=True
    )
    assert set(out) == {"hits", "pack", "timings"}
    assert "pack" in out["timings"]["stages"]


def test_keyword_mode_timings(vault: Path) -> None:
    out = commands.op_find(
        vault, query="metabolism", mode="keyword", include_timings=True
    )
    assert "keyword" in out["timings"]["stages"]


def test_compact_detail_carries_the_excerpt_and_omits_signals(vault: Path) -> None:
    full = commands.op_find(vault, query="metabolism")
    compact = commands.op_find(vault, query="metabolism", detail="compact")
    assert [h["path"] for h in compact] == [h["path"] for h in full]
    for h, f in zip(compact, full, strict=True):
        # Compact may cut the excerpt; it never rewrites what the full hit says.
        assert h["excerpt"]
        assert " ".join(f["excerpt"].split()).startswith(h["excerpt"].removesuffix(" …"))
        assert "signals" not in h
        assert h["path"] and "title" in h and "updated" in h and "type" in h


def test_compact_excerpt_is_cut_at_a_word_boundary() -> None:
    text = " ".join(["wordwordword"] * 60)
    hit = Hit(path="Notes/a.md", type="insight", scope=None, title="A", updated="2026-01-01", excerpt=text)

    full = egress.project(hit, egress.LEVEL_FULL)
    compact = egress.project(hit, egress.LEVEL_FULL, compact=True)

    assert full is not None and compact is not None
    assert full["excerpt"] == text
    assert compact["excerpt"].endswith(" …")
    kept = compact["excerpt"].removesuffix(" …")
    assert len(kept) <= egress.COMPACT_HIT_EXCERPT_MAX
    assert text.startswith(kept) and text[len(kept)] == " "


def test_compact_preserves_lifecycle_fields(vault: Path) -> None:
    # Superseded tombstones must stay recognizable in the compact shape.
    full = commands.op_find(vault, query="", limit=100)
    compact = commands.op_find(vault, query="", limit=100, detail="compact")
    full_flags = {h["path"]: h.get("status") for h in full}
    for h in compact:
        assert h.get("status") == full_flags[h["path"]]


def test_compact_composes_with_pack(vault: Path) -> None:
    out = commands.op_find(vault, query="metabolism", detail="compact", pack=True)
    assert set(out) == {"hits", "pack"}
    assert all("signals" not in h for h in out["hits"])


def test_invalid_detail_rejected(vault: Path) -> None:
    with pytest.raises(ValueError, match="detail"):
        commands.op_find(vault, query="metabolism", detail="verbose")


def test_hit_compact_dict_direct(vault: Path) -> None:
    hits = find_module.find(vault, query="metabolism")
    assert hits
    c = hits[0].as_compact_dict()
    assert "signals" not in c
    assert c["excerpt"] == hits[0].excerpt
    assert c["path"] == hits[0].path


def test_log_find_call_accepts_timing_summary() -> None:
    # Signature-level check; logging itself is env-disabled in the suite.
    query_log.log_find_call(
        query="q", mode="hybrid", scope="kb", types=None, projects=None,
        tags=None, limit=5, rerank=False, prefer_compiled=True, graph=True,
        hits=[], timing_summary={"total_ms": 1.2, "cache_hit": False},
    )
