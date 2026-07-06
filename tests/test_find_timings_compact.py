"""Opt-in `find` timing diagnostics + compact/full result detail.

Default behavior must stay byte-identical: no envelope, full hit dicts with
excerpts. Timings are opt-in via `include_timings`, compact via `detail`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import commands, query_log
from exomem import find as find_module


def test_default_find_shape_unchanged(vault: Path) -> None:
    out = commands.op_find(vault, query="metabolism")
    assert isinstance(out, list)
    assert out, "fixture vault should match 'metabolism'"
    assert "excerpt" in out[0]
    assert "timings" not in out[0]


def test_timings_envelope_and_stages(vault: Path) -> None:
    out = commands.op_find(vault, query="metabolism", include_timings=True)
    assert isinstance(out, dict)
    assert set(out) == {"hits", "timings"}
    t = out["timings"]
    assert t["total_ms"] >= 0
    assert "cache" in t and "hit" in t["cache"]
    assert t["profile"]["mode"] == "hybrid"
    assert t["profile"]["detail"] == "full"
    assert t["profile"]["pack"] is False
    assert t["profile"]["auto_rerank"] is True
    assert t["profile"]["compute_policy"]["mode"] in {"quiet", "normal", "performance"}
    stages = t["stages"]
    # Lexical lanes always run in hybrid mode; vector is present as a timed
    # or errored stage even with embeddings disabled (soft-fail, never fatal).
    assert "bm25" in stages
    assert "keyword" in stages
    assert "vector" in stages
    assert "fusion" in stages
    assert "filter_hits" in stages
    assert "serialize" in stages
    # No content leaks into diagnostics: numbers, flags, short names only.
    for entry in stages.values():
        for key in entry:
            assert key in ("ms", "skipped", "error")


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


def test_compact_detail_omits_excerpt_and_signals(vault: Path) -> None:
    full = commands.op_find(vault, query="metabolism")
    compact = commands.op_find(vault, query="metabolism", detail="compact")
    assert [h["path"] for h in compact] == [h["path"] for h in full]
    for h in compact:
        assert "excerpt" not in h
        assert "signals" not in h
        assert h["path"] and "title" in h and "updated" in h and "type" in h


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
    assert all("excerpt" not in h for h in out["hits"])


def test_invalid_detail_rejected(vault: Path) -> None:
    with pytest.raises(ValueError, match="detail"):
        commands.op_find(vault, query="metabolism", detail="verbose")


def test_hit_compact_dict_direct(vault: Path) -> None:
    hits = find_module.find(vault, query="metabolism")
    assert hits
    c = hits[0].as_compact_dict()
    assert "excerpt" not in c and "signals" not in c
    assert c["path"] == hits[0].path


def test_log_find_call_accepts_timing_summary() -> None:
    # Signature-level check; logging itself is env-disabled in the suite.
    query_log.log_find_call(
        query="q", mode="hybrid", scope="kb", types=None, projects=None,
        tags=None, limit=5, rerank=False, prefer_compiled=True, graph=True,
        hits=[], timing_summary={"total_ms": 1.2, "cache_hit": False},
    )
