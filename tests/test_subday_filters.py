"""Sub-day retrieval bounds, and the ambiguity they must not hide.

`updated_after` / `updated_before` accept an instant as well as a day. The
interesting case is the boundary day: a page recorded only as `2026-08-05` is
genuinely unordered against `2026-08-05T09:00:00Z`, because the day denotes an
unknown instant within it.

Dropping such a page silently is the exact defect this change exists to fix.
Including it silently reports a guess as a fact. So it comes back marked.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import yaml

from exomem.structured_filters import (
    FilterShortcuts,
    compile_filter,
    evaluate_filter,
    indeterminate_bounds,
    page_view,
)


def _page(frontmatter_line: str) -> dict:
    return page_view(
        SimpleNamespace(frontmatter=yaml.safe_load(frontmatter_line), file_kind="note")
    )


DAY_ONLY = "updated: 2026-08-05"
DAY_BEFORE = "updated: 2026-08-04"
DAY_AFTER = "updated: 2026-08-06"
PRECISE_LATE = "updated: 2026-08-05T11:00:00Z"
PRECISE_EARLY = "updated: 2026-08-05T08:00:00Z"

BOUND = "2026-08-05T09:00:00Z"


# --- the bound is decidable --------------------------------------------------


def test_precise_page_after_the_bound_is_returned() -> None:
    plan = compile_filter(None, shortcuts=FilterShortcuts(updated_after=BOUND))
    assert evaluate_filter(plan, page=_page(PRECISE_LATE))


def test_precise_page_before_the_bound_is_excluded() -> None:
    plan = compile_filter(None, shortcuts=FilterShortcuts(updated_after=BOUND))
    assert not evaluate_filter(plan, page=_page(PRECISE_EARLY))


def test_day_only_page_on_an_earlier_day_is_excluded() -> None:
    """A whole day orders against an instant outside it — no ambiguity here."""
    plan = compile_filter(None, shortcuts=FilterShortcuts(updated_after=BOUND))
    assert not evaluate_filter(plan, page=_page(DAY_BEFORE))


def test_day_only_page_on_a_later_day_is_returned() -> None:
    plan = compile_filter(None, shortcuts=FilterShortcuts(updated_after=BOUND))
    assert evaluate_filter(plan, page=_page(DAY_AFTER))


# --- the bound is not decidable ---------------------------------------------


def test_day_only_page_on_the_boundary_day_is_returned() -> None:
    """Not dropped: that is the failure mode this whole change is about."""
    plan = compile_filter(None, shortcuts=FilterShortcuts(updated_after=BOUND))
    assert evaluate_filter(plan, page=_page(DAY_ONLY))


def test_day_only_page_on_the_boundary_day_is_marked() -> None:
    """Not silently included either: the caller must be able to see the guess."""
    assert indeterminate_bounds(
        _page(DAY_ONLY), shortcuts=FilterShortcuts(updated_after=BOUND)
    ) == ("updated_after",)


@pytest.mark.parametrize(
    "frontmatter_line",
    [PRECISE_LATE, PRECISE_EARLY, DAY_BEFORE, DAY_AFTER],
)
def test_decidable_pages_are_never_marked(frontmatter_line: str) -> None:
    assert indeterminate_bounds(
        _page(frontmatter_line), shortcuts=FilterShortcuts(updated_after=BOUND)
    ) == ()


def test_updated_before_marks_the_same_boundary_day() -> None:
    assert indeterminate_bounds(
        _page(DAY_ONLY), shortcuts=FilterShortcuts(updated_before=BOUND)
    ) == ("updated_before",)


def test_both_bounds_can_be_indeterminate_at_once() -> None:
    marked = indeterminate_bounds(
        _page(DAY_ONLY),
        shortcuts=FilterShortcuts(updated_after=BOUND, updated_before="2026-08-05T17:00:00Z"),
    )
    assert set(marked) == {"updated_after", "updated_before"}


# --- day-precision bounds stay total ----------------------------------------


def test_a_day_precision_bound_is_never_indeterminate() -> None:
    """Precision of the *question* decides the comparison, not the data.

    "Was this updated on or after 2026-08-05?" is fully answerable for a page
    recorded to the second, so a day-scoped bound must not start reporting
    ambiguity just because some pages are precise.
    """
    for line in (DAY_ONLY, PRECISE_EARLY, PRECISE_LATE, DAY_BEFORE, DAY_AFTER):
        assert indeterminate_bounds(
            _page(line), shortcuts=FilterShortcuts(updated_after="2026-08-05")
        ) == (), line


def test_day_precision_bound_still_filters_by_day() -> None:
    plan = compile_filter(None, shortcuts=FilterShortcuts(updated_after="2026-08-05"))
    assert evaluate_filter(plan, page=_page(DAY_ONLY))
    assert evaluate_filter(plan, page=_page(PRECISE_EARLY))
    assert not evaluate_filter(plan, page=_page(DAY_BEFORE))


def test_recency_days_stays_day_scoped() -> None:
    """`recency_days` is a day window by construction and gains no ambiguity."""
    assert indeterminate_bounds(
        _page(DAY_ONLY), shortcuts=FilterShortcuts(recency_days=3650)
    ) == ()


# --- a page with no recorded date -------------------------------------------


def test_a_page_without_a_recorded_date_is_excluded_not_marked() -> None:
    """Absent is not ambiguous: there is nothing to be uncertain between."""
    page = page_view(SimpleNamespace(frontmatter={}, file_kind="note"))
    plan = compile_filter(None, shortcuts=FilterShortcuts(updated_after=BOUND))
    assert not evaluate_filter(plan, page=page)
    assert indeterminate_bounds(page, shortcuts=FilterShortcuts(updated_after=BOUND)) == ()


# --- end to end through find() ----------------------------------------------


def test_find_marks_boundary_day_hits_and_leaves_decided_ones_clean(vault) -> None:
    """The flag has to reach the caller, or the ambiguity is still hidden."""
    from exomem import find as find_module

    pages = {
        "day-only": "2026-08-05",
        "precise-after": "2026-08-05T11:00:00Z",
    }
    for slug, updated in pages.items():
        path = vault / f"Knowledge Base/Notes/Insights/{slug}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"---\ntype: insight\nstatus: active\ncreated: {updated}\n"
            f"updated: {updated}\n---\n\n# {slug}\n\nBoundary probe body.\n",
            encoding="utf-8",
        )
    find_module.clear_cache()

    result = find_module.find(
        vault, query="boundary probe", mode="keyword", limit=20,
        updated_after="2026-08-05T09:00:00Z",
    )
    marks = {
        hit.path.rsplit("/", 1)[-1].removesuffix(".md"): hit.order_indeterminate
        for hit in result
    }
    assert marks.get("day-only") == ["updated_after"], marks
    assert marks.get("precise-after") == [], marks


def test_find_payload_carries_the_flag_only_when_earned(vault) -> None:
    from exomem import find as find_module

    path = vault / "Knowledge Base/Notes/Insights/payload-probe.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\ntype: insight\nstatus: active\ncreated: 2026-08-05\n"
        "updated: 2026-08-05\n---\n\n# payload probe\n\nPayload probe body.\n",
        encoding="utf-8",
    )
    find_module.clear_cache()

    vague = find_module.find(
        vault, query="payload probe", mode="keyword", limit=20,
        updated_after="2026-08-05T09:00:00Z",
    )
    assert any(
        h.as_dict().get("order_indeterminate") == ["updated_after"] for h in vague
    )

    find_module.clear_cache()
    decided = find_module.find(
        vault, query="payload probe", mode="keyword", limit=20,
        updated_after="2026-08-05",
    )
    assert all("order_indeterminate" not in h.as_dict() for h in decided)
