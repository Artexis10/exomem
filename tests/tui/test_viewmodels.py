"""Pure renderer/view-model helpers, tested without a running app."""

from __future__ import annotations

import pytest

pytest.importorskip("textual")

from exomem.tui.backend import AskOutcome, ensure_semantic_unit  # noqa: E402
from exomem.tui.screens.ask import fit, marker_summary, pack_to_text, short_path  # noqa: E402
from exomem.tui.screens.continue_ import checkpoint_age  # noqa: E402
from exomem.tui.screens.home import overview_strip, summarize_overview  # noqa: E402
from exomem.tui.theme import GLYPHS_ASCII, GLYPHS_UNICODE, pick_glyphs  # noqa: E402

GLYPHS = GLYPHS_UNICODE


def test_summarize_overview_warns_carry_next_actions():
    sections = {
        "mode": {"ok": True, "data": {"mode": "normal"}},
        "readiness": {"ok": True, "data": {"warming": True, "pending": ["embeddings"]}},
        "attention": {"ok": True, "data": {"all_total": 3}},
        "packs": {"ok": True, "data": {"selected": []}},
        "hooks": {"ok": True, "data": {"success": False}},
    }
    lines = [str(line) for line in summarize_overview(sections, GLYPHS)]
    joined = "\n".join(lines)
    assert "warming" in joined
    assert "3 item(s)" in joined and "press 4" in joined
    assert "install-hook" in joined
    # a failed section degrades with a pointer, never crashes
    sections["mode"] = {"ok": False, "error": {"code": "INTERNAL"}}
    joined = "\n".join(str(line) for line in summarize_overview(sections, GLYPHS))
    assert "unavailable" in joined


def test_overview_strip_is_single_line():
    sections = {
        "readiness": {"ok": True, "data": {"warming": False}},
        "attention": {"ok": True, "data": {"all_total": 0}},
        "packs": {"ok": True, "data": {"selected": ["technical"]}},
    }
    strip = str(overview_strip(sections, GLYPHS))
    assert "\n" not in strip
    assert "ready" in strip and "technical" in strip


def test_marker_summary_states_warming_and_skips():
    outcome = AskOutcome(
        hits=[], warming={"components": ["embeddings"], "since_s": 7}, degraded=["clip"]
    )
    summary = marker_summary(outcome)
    assert "warming" in summary and "embeddings" in summary and "clip" in summary
    assert marker_summary(AskOutcome(hits=[])) == ""


def test_pack_to_text_flattens_lists():
    text = pack_to_text({"claims": ["a", "b"], "outline": "one section"})
    assert "## claims" in text and "- a" in text and "one section" in text


def test_short_path_and_fit():
    long_path = "Knowledge Base/Notes/Research/some-project/very-long-note-name-goes-here.md"
    shortened = short_path(long_path, max_len=30)
    assert shortened.startswith("…/") and shortened.endswith(".md")
    assert short_path("short.md") == "short.md"
    assert fit("abcdef", 4) == "abc…"
    assert fit("abc", 4) == "abc"


def test_checkpoint_age_buckets():
    now = 10**18
    assert checkpoint_age(now - 5 * 60 * 10**9, now).endswith("m ago")
    assert checkpoint_age(now - 5 * 3600 * 10**9, now).endswith("h ago")
    assert checkpoint_age(now - 3 * 86400 * 10**9, now).endswith("d ago")
    assert checkpoint_age(None, now) == "unknown age"


def test_pick_glyphs_falls_back_to_ascii():
    assert pick_glyphs("utf-8") == GLYPHS_UNICODE
    assert pick_glyphs("ascii") == GLYPHS_ASCII
    assert pick_glyphs(None) == GLYPHS_ASCII


def test_ensure_semantic_unit_appends_only_when_missing():
    wrapped = ensure_semantic_unit("plain prose.", "My Conclusion")
    assert "## Observations" in wrapped and "[insight] My Conclusion" in wrapped
    already = "body\n\n## Observations\n- [decision] keep it\n"
    assert ensure_semantic_unit(already, "t") == already
    rich = "## Decision\n\nA substantive body.\n"
    assert ensure_semantic_unit(rich, "t") == rich
