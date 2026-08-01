"""Pure renderers and view-model helpers, tested without a running app."""

from __future__ import annotations

import pytest

pytest.importorskip("textual")

from exomem.tui.backend import AskOutcome, ensure_semantic_unit  # noqa: E402
from exomem.tui.format import (  # noqa: E402
    first_words,
    fit,
    label_field,
    truncate_path,
    wrap,
)
from exomem.tui.screens.ask import hit_rows, pack_to_text, warming_lanes  # noqa: E402
from exomem.tui.screens.capture import derive_title, first_observation  # noqa: E402
from exomem.tui.screens.continue_ import checkpoint_age  # noqa: E402
from exomem.tui.screens.home import health_lines, status_lines  # noqa: E402
from exomem.tui.screens.review import item_kind, item_why, queue_rows  # noqa: E402
from exomem.tui.theme import (  # noqa: E402
    GLYPHS_ASCII,
    GLYPHS_UNICODE,
    make_skin,
    no_color_requested,
    pick_glyphs,
)
from exomem.tui.widgets import BarRow, continuation, receipt  # noqa: E402
from rich.text import Text  # noqa: E402

SKIN = make_skin(GLYPHS_UNICODE)
MONO = make_skin(GLYPHS_ASCII, color=False)

READY_SECTIONS = {
    "mode": {"ok": True, "data": {"mode": "normal"}},
    "readiness": {"ok": True, "data": {"warming": False}},
    "attention": {"ok": True, "data": {"all_total": 2, "state_summary": {"open": 2}}},
    "packs": {"ok": True, "data": {"selected": ["technical", "business"]}},
    "hooks": {"ok": True, "data": {"success": True}},
    "corpus": {"ok": True, "data": {"notes": 132, "known": True}},
}


# -- overflow rules --------------------------------------------------------- #
def test_fit_truncates_at_the_tail():
    assert fit("abcdef", 4) == "abc…"
    assert fit("abc", 4) == "abc"
    assert fit("anything", 0) == ""


def test_paths_truncate_from_the_left_so_the_filename_survives():
    path = "Knowledge Base/Notes/Insights/queue-backpressure-needs-explicit-limits.md"
    shortened = truncate_path(path, 46)
    assert shortened.startswith("…/")
    assert shortened.endswith("limits.md"), "the filename must never be the part cut"
    assert len(shortened) <= 46
    assert truncate_path("short.md", 40) == "short.md"
    # even below one segment, the tail of the filename wins
    assert truncate_path(path, 12).endswith("limits.md")


def test_wrap_breaks_on_words_not_mid_word():
    lines = wrap("bounded queues shed load predictably under pressure", 20)
    assert all(len(line) <= 20 for line in lines)
    assert "predictably" in " ".join(lines)


def test_label_field_pads_to_the_receipt_column():
    assert label_field("✓", "vault") == "✓ vault     "
    assert len(label_field("✓", "vault")) == 12


def test_first_words_is_bounded():
    assert first_words("a b c") == "a b c"
    assert first_words("x" * 60, 10).endswith("…")


# -- receipts --------------------------------------------------------------- #
def test_receipt_is_one_row_with_glyph_and_word():
    line = receipt(SKIN, "ok", "ready", "retrieval warm · 132 notes", budget=60)
    plain = line.plain
    assert "\n" not in plain
    assert plain.startswith("● ready")
    assert "retrieval warm" in plain
    assert len(plain) <= 60


def test_receipt_right_column_is_pushed_to_the_budget_edge():
    line = receipt(SKIN, "warn", "review", "2 items need attention", right="4 open", budget=60)
    assert line.plain.rstrip().endswith("4 open")
    assert len(line.plain) <= 60


def test_receipt_truncates_the_detail_not_the_status():
    line = receipt(SKIN, "fail", "not saved", "x" * 200, budget=40)
    assert line.plain.startswith("✗ not saved")
    assert len(line.plain) <= 40


def test_continuation_line_is_dim_and_prefixed():
    assert continuation(SKIN, "nothing to do").plain.strip().startswith("→")


# -- skins: NO_COLOR degrades losslessly ------------------------------------ #
def test_status_is_glyph_plus_word_in_every_skin():
    for skin in (SKIN, MONO):
        for state in ("ok", "warn", "fail", "done", "idle"):
            glyph, _style = skin.status(state)
            assert glyph, "every status needs a glyph, since color may be absent"
    assert MONO.status("ok")[1] == "", "the mono skin carries no hue at all"
    assert MONO.status("ok")[0] == "*"


def test_no_color_requested_follows_the_convention(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    assert no_color_requested() is False
    monkeypatch.setenv("NO_COLOR", "")
    assert no_color_requested() is True, "NO_COLOR set to anything means no color"


def test_pick_glyphs_falls_back_to_ascii():
    assert pick_glyphs("utf-8") == GLYPHS_UNICODE
    assert pick_glyphs("ascii") == GLYPHS_ASCII
    assert pick_glyphs(None) == GLYPHS_ASCII


def test_ascii_glyph_set_covers_every_unicode_glyph():
    assert set(GLYPHS_ASCII) == set(GLYPHS_UNICODE)
    assert all(value.isascii() for value in GLYPHS_ASCII.values())


# -- selection bar ---------------------------------------------------------- #
def test_bar_row_puts_the_bar_in_column_zero_of_every_line():
    row = BarRow("x", [(1, Text("title")), (3, Text("meta"))])
    selected = row.render(SKIN, True).plain.splitlines()
    unselected = row.render(SKIN, False).plain.splitlines()
    assert [line[0] for line in selected] == ["▌", "▌"]
    assert [line[0] for line in unselected] == [" ", " "]
    assert selected[0][1:] == unselected[0][1:], "only the bar cell differs"


def test_bar_row_degrades_to_ascii():
    row = BarRow("x", [(1, Text("title"))])
    assert row.render(MONO, True).plain.startswith(">")


# -- Home ------------------------------------------------------------------- #
def test_status_lines_lead_with_readiness_then_review():
    lines = [line.plain for line in status_lines(READY_SECTIONS, SKIN, 76)]
    assert lines[0].startswith("● ready")
    assert "132 notes" in lines[0]
    assert any(line.startswith("▲ review") for line in lines)
    assert all("\n" not in line for line in lines)


def test_status_lines_warming_carries_its_own_next_step():
    sections = dict(READY_SECTIONS)
    sections["readiness"] = {"ok": True, "data": {"warming": True, "pending": ["embeddings"]}}
    lines = [line.plain for line in status_lines(sections, SKIN, 76)]
    assert lines[0].startswith("▲ warming") and "embeddings" in lines[0]
    assert "automatically" in lines[1]


def test_health_lines_cover_every_lane():
    joined = "\n".join(line.plain for line in health_lines(READY_SECTIONS, SKIN, 56))
    for word in ("mode", "retrieval", "review", "packs", "hooks"):
        assert word in joined


def test_health_lines_name_the_hook_command_when_unwired():
    sections = dict(READY_SECTIONS)
    sections["hooks"] = {"ok": True, "data": {"success": False}}
    joined = "\n".join(line.plain for line in health_lines(sections, SKIN, 56))
    assert "install-hook" in joined


# -- Ask -------------------------------------------------------------------- #
def test_hit_rows_are_two_lines_with_identity_and_path():
    rows = hit_rows(
        [
            {
                "title": "Queue backpressure needs explicit limits",
                "type": "insight",
                "updated": "2026-07-20",
                "path": "Knowledge Base/Notes/Insights/queue-backpressure.md",
            }
        ],
        SKIN,
        76,
    )
    lines = rows[0].render(SKIN, False).plain.splitlines()
    assert len(lines) == 2
    assert "Queue backpressure" in lines[0]
    assert "insight" in lines[1] and "2026-07-20" in lines[1]
    assert all(len(line) <= 76 for line in lines)


def test_warming_lanes_names_components():
    assert warming_lanes(AskOutcome(warming={"components": ["embeddings", "reranker"]})) == [
        "embeddings",
        "reranker",
    ]
    assert warming_lanes(AskOutcome()) == []


def test_pack_to_text_flattens_lists():
    text = pack_to_text({"claims": ["a", "b"], "outline": "one section"})
    assert "## claims" in text and "- a" in text and "one section" in text


# -- Capture ---------------------------------------------------------------- #
def test_derive_title_uses_the_first_real_line():
    assert derive_title("\n\n# A heading\nmore") == "A heading"
    assert derive_title("x" * 200).endswith("…")
    assert derive_title("   ") == ""


def test_first_observation_reads_the_semantic_unit():
    assert first_observation("body\n\n## Observations\n- [finding] needs a floor\n") == (
        "finding",
        "needs a floor",
    )
    assert first_observation("no units here") is None


# -- Review ----------------------------------------------------------------- #
def test_item_kind_translates_categories_into_measured_words():
    assert item_kind({"categories": ["corpus_contradictions"]}) == ("contradiction", "warn")
    assert item_kind({"categories": ["unprocessed_source"]}) == ("unprocessed", "idle")
    assert item_kind({"categories": ["something_new"], "severity": "warning"})[1] == "warn"


def test_queue_rows_show_the_measured_why_then_the_path():
    item = {
        "ref": "exomem://review/aa",
        "categories": ["corpus_contradictions"],
        "reasons": [{"detail": "a newer note reaches the opposite conclusion"}],
        "path": "Knowledge Base/Notes/Insights/queue-backpressure.md",
    }
    assert item_why(item).startswith("a newer note")
    lines = queue_rows([item], {}, SKIN, 76)[0].render(SKIN, False).plain.splitlines()
    assert "contradiction" in lines[0] and "opposite conclusion" in lines[0]
    assert lines[1].strip().endswith("queue-backpressure.md")


def test_triaged_rows_stay_in_place_struck_with_a_way_back():
    item = {
        "ref": "exomem://review/aa",
        "categories": ["corpus_contradictions"],
        "reasons": [{"detail": "a newer note reaches the opposite conclusion"}],
        "path": "Knowledge Base/Notes/Insights/queue-backpressure.md",
    }
    rows = queue_rows([item], {"exomem://review/aa": "dismissed"}, SKIN, 76)
    rendered = rows[0].render(SKIN, False)
    assert "dismissed" in rendered.plain
    assert "o reopens" in rendered.plain
    assert any("strike" in str(span.style) for span in rendered.spans), (
        "a triaged receipt must read as struck, not merely dim"
    )


# -- Continue --------------------------------------------------------------- #
def test_checkpoint_age_buckets():
    now = 10**18
    assert checkpoint_age(now - 5 * 60 * 10**9, now).endswith("m ago")
    assert checkpoint_age(now - 5 * 3600 * 10**9, now).endswith("h ago")
    assert checkpoint_age(now - 3 * 86400 * 10**9, now).endswith("d ago")
    assert checkpoint_age(None, now) == "unknown age"


# -- governed content ------------------------------------------------------- #
def test_ensure_semantic_unit_appends_only_when_missing():
    wrapped = ensure_semantic_unit("plain prose.", "My Conclusion")
    assert "## Observations" in wrapped and "[insight] My Conclusion" in wrapped
    already = "body\n\n## Observations\n- [decision] keep it\n"
    assert ensure_semantic_unit(already, "t") == already
    rich = "## Decision\n\nA substantive body.\n"
    assert ensure_semantic_unit(rich, "t") == rich


# -- colour depth: the palette has to survive quantisation ------------------- #
def _to_256(hex_colour: str) -> tuple[int, str]:
    """The 256-colour cell a truecolour value lands on, and its real value."""
    from rich.color import Color as RichColor
    from rich.color_triplet import ColorTriplet

    triplet = ColorTriplet(*(int(hex_colour[index : index + 2], 16) for index in (1, 3, 5)))
    downgraded = RichColor.from_triplet(triplet).downgrade(2)
    exact = downgraded.get_truecolor()
    return downgraded.number, f"#{exact.red:02x}{exact.green:02x}{exact.blue:02x}"


def test_selection_fill_gains_no_hue_it_was_not_given():
    """Regression: a 22% amber blend quantised to pure dark red (#5f0000).

    The colour cube floors each channel independently, so `#3f2d06` — the
    blend the design's "accent at 12-22%" produces — lost its green entirely
    and the selected row rendered red. The fill must therefore land somewhere
    that cannot invent a hue: a near-neutral resolves against the greyscale
    ramp, where channel collapse is impossible by construction.
    """
    from exomem.tui.theme import SELECTION_BG

    def spread(hex_colour: str) -> int:
        channels = [int(hex_colour[i : i + 2], 16) for i in (1, 3, 5)]
        return max(channels) - min(channels)

    number, exact = _to_256(SELECTION_BG)
    assert spread(exact) <= 16, (
        f"{SELECTION_BG} quantises to {exact} (cell {number}) — a hue the fill never had"
    )
    # the blend that caused the bug is still caught by exactly this rule
    assert _to_256("#3f2d06")[1] == "#5f0000"
    assert spread(_to_256("#3f2d06")[1]) > 16


def test_accent_and_text_roles_survive_quantisation():
    from exomem.tui.theme import ACCENT, SECONDARY, TEXT

    assert _to_256(ACCENT)[0] == 214, "the brand amber must land on ANSI 214"
    # supporting copy has to stay clearly brighter than the background
    assert int(_to_256(SECONDARY)[1][1:3], 16) >= 0x80
    assert int(_to_256(TEXT)[1][1:3], 16) >= 0xC0


def test_selection_fill_leaves_the_row_readable():
    """A saturated fill destroyed the row's own text hierarchy.

    Cube entry 94 (#875f00) scored 1.89:1 against secondary text, which is
    what made selected rows look muddy. The fill has to stay legible against
    every foreground role it can sit behind.
    """
    from exomem.tui.theme import SECONDARY, SELECTION_BG, TEXT

    def luminance(hex_colour: str) -> float:
        def channel(index: int) -> float:
            value = int(hex_colour[index : index + 2], 16) / 255
            return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4

        return 0.2126 * channel(1) + 0.7152 * channel(3) + 0.0722 * channel(5)

    def contrast(a: str, b: str) -> float:
        high, low = sorted((luminance(a), luminance(b)), reverse=True)
        return (high + 0.05) / (low + 0.05)

    fill = _to_256(SELECTION_BG)[1]
    assert contrast(fill, _to_256(TEXT)[1]) >= 7.0
    assert contrast(fill, _to_256(SECONDARY)[1]) >= 4.5
    # the saturated fill this replaced must still fail the same rule
    assert contrast("#875f00", _to_256(SECONDARY)[1]) < 3.0


def test_selected_rows_lift_metadata_out_of_dim():
    from rich.text import Text

    from exomem.tui.widgets import BarRow

    row = BarRow("x", [(1, Text("title", style=SKIN.text)), (3, Text("meta", style=SKIN.dim))])
    selected = row.render(SKIN, True)
    assert SKIN.dim not in [span.style for span in selected.spans], (
        "dim recedes past legibility against the lit row"
    )
    assert SKIN.dim in [span.style for span in row.render(SKIN, False).spans]


def test_a_long_status_word_never_collides_with_its_detail():
    """Regression: `✗ not scannedTest is not a directory` on one row.

    `ljust` is a no-op once the label already exceeds the field, so any status
    word longer than the receipt column ran straight into its own message.
    """
    from exomem.tui.format import label_field

    for word in ("vault", "not scanned", "recall failed", "queue unavailable"):
        assert label_field("✗", word).endswith(" "), f"{word!r} loses its separator"
    line = receipt(SKIN, "fail", "not scanned", "/tmp/Test — no folder there", budget=76)
    assert "not scanned /tmp" in line.plain or "not scanned  " in line.plain
    assert "scannedTest" not in line.plain


def test_selection_fill_is_exactly_neutral():
    """No hue at any depth: the accent lives in the bar, not the fill."""
    from exomem.tui.theme import SELECTION_BG

    channels = [int(SELECTION_BG[i : i + 2], 16) for i in (1, 3, 5)]
    assert max(channels) == min(channels), f"{SELECTION_BG} carries a tint"
    assert _to_256(SELECTION_BG)[1] == SELECTION_BG, "and it survives quantisation unchanged"


def test_context_pane_wraps_its_prose_instead_of_cutting_it():
    """Regression: `bound to the fingerp…` hid what the action binds to."""
    from exomem.tui.screens.review import context_lines

    item = {
        "ref": "exomem://review/aa",
        "categories": ["unprocessed_source"],
        "reasons": [{"detail": "captured but never compiled into a conclusion"}],
        "path": "Knowledge Base/Sources/2026/08/a-note.md",
        "fingerprint": "788e107a99",
    }
    rendered = context_lines(item, {"target": {"path": item["path"]}}, SKIN, 54).plain
    flowed = " ".join(rendered.split())
    assert "bound to the fingerprint" in flowed, "the binding must survive the column"
    assert "exomem serve http, then /studio/" in flowed
    assert "fingerp\u2026" not in rendered, "prose must wrap rather than lose its tail"
    assert all(len(line) <= 56 for line in rendered.splitlines())
