"""The import index that `plan` reads is remembered per process, so a later page does not read every Source again.

A stale index would let a page file a Source that an earlier import already holds, so each test names what the
cache must still notice. The Source pages here are written by hand with the front matter an import leaves.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from test_adoption_paging import _vault

from exomem import adoption_plan, adoption_run


def _source(vault: Path, name: str, original: str, text: str = "# Page\n") -> Path:
    page = vault / "Knowledge Base" / "Sources" / f"{name}.md"
    page.write_text(
        f"---\nimported_from: {original}\noriginal_sha256: {'0' * 64}\ncaptured: 2026-10-27\n---\n{text}",
        encoding="utf-8",
    )
    return page


@pytest.fixture
def reads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The Source pages that the index parsed, in order; each test starts from a process that has read none."""
    adoption_plan._SOURCE_PAGES.clear()
    # A page written a moment ago counts as unchanged-since-read here, which a real vault proves by waiting out the window.
    monkeypatch.setattr(adoption_plan, "_RACY_NS", 0)
    parsed: list[str] = []
    real = adoption_plan.page_import_entries

    def counting(root: Path, page: Path):
        if page.name != "index.md":  # the Sources index holds no import, and every filing rewrites it
            parsed.append(page.name)
        return real(root, page)

    monkeypatch.setattr(adoption_plan, "page_import_entries", counting)
    return parsed


def _originals(index: list[adoption_plan.EarlierImport]) -> list[str]:
    return sorted(entry.imported_from for entry in index)


def test_a_second_read_parses_only_the_source_page_that_changed_and_drops_the_one_that_is_gone(
    tmp_path: Path, reads: list[str]
) -> None:
    """Catches an index that serves a page's old original after the page changed, or one that keeps a deleted Source."""
    vault = _vault(tmp_path)
    for name in ("a", "b", "c"):
        _source(vault, name, f"Legacy/{name}.md")

    assert _originals(adoption_plan.read_import_index(vault)) == ["Legacy/a.md", "Legacy/b.md", "Legacy/c.md"]
    assert sorted(reads) == ["a.md", "b.md", "c.md"]

    reads.clear()
    adoption_plan.read_import_index(vault)
    assert reads == []  # nothing moved, so nothing is parsed again

    _source(vault, "b", "Legacy/b-renamed.md", text="# Page, longer\n")
    (vault / "Knowledge Base" / "Sources" / "c.md").unlink()
    index = adoption_plan.read_import_index(vault)

    assert reads == ["b.md"]
    assert _originals(index) == ["Legacy/a.md", "Legacy/b-renamed.md"]


def test_a_read_that_stops_keeps_the_pages_it_parsed_so_each_call_moves_on_by_its_floor(
    tmp_path: Path, reads: list[str]
) -> None:
    """Catches a read that starts over at each call, so a call with no room never gets past the first pages."""
    vault = _vault(tmp_path)
    for number in range(5):
        _source(vault, f"page-{number}", f"Legacy/{number}.md")

    first = adoption_plan.read_import_index_until(vault, lambda: True, floor=2)
    second = adoption_plan.read_import_index_until(vault, lambda: True, floor=2)
    third = adoption_plan.read_import_index_until(vault, lambda: True, floor=2)

    assert (first, second) == (None, None)
    assert len(reads) == 5 and len(set(reads)) == 5
    assert _originals(third) == [f"Legacy/{number}.md" for number in range(5)]


def test_a_source_page_that_the_filesystem_refuses_for_now_ends_the_read_and_is_read_again_next_time(
    tmp_path: Path, reads: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a refused read that counts as a page with no imports: the plan would file a duplicate of that Source."""
    vault = _vault(tmp_path)
    for number in range(3):
        _source(vault, f"page-{number}", f"Legacy/{number}.md")
    real = adoption_plan.page_import_entries
    refusing = [True]

    def refused(root: Path, page: Path):
        if refusing[0] and page.name == "page-1.md":
            raise adoption_plan.reserved_paths.ReservedPathLeafError("CAPABILITY_UNAVAILABLE")
        return real(root, page)

    monkeypatch.setattr(adoption_plan, "page_import_entries", refused)

    first = adoption_plan.read_import_index_until(vault, lambda: False, floor=0)
    refusing[0] = False
    second = adoption_plan.read_import_index_until(vault, lambda: False, floor=0)

    assert first is None  # not an index that lacks the page
    assert second is not None and _originals(second) == ["Legacy/0.md", "Legacy/1.md", "Legacy/2.md"]


def test_an_apply_that_reads_the_sources_over_many_calls_shows_how_far_the_read_has_reached(
    tmp_path: Path, reads: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches an apply whose cold read of the Sources changes nothing a caller sees, so a driver takes it for a stall."""
    vault = _vault(tmp_path)
    (vault / "Legacy").mkdir()
    (vault / "Legacy" / "a.md").write_text("# A\n\nBody.\n", encoding="utf-8")
    run_id = adoption_run.start(vault, path="Legacy", consume=False)["run_id"]
    adoption_run.select(vault, run_id=run_id, include=["Legacy"])
    plan_id = adoption_run.plan(vault, run_id=run_id)["plan"]["plan_id"]
    for number in range(6):  # filed by someone else after the plan: its seal breaks, so only a full read can tell
        _source(vault, f"later-{number}", f"Elsewhere/{number}.md")
    adoption_plan._SOURCE_PAGES.clear()  # a process that has read no Source yet
    monkeypatch.setattr(adoption_plan, "INDEX_READ_FLOOR", 1)
    monkeypatch.setattr(adoption_run, "_writer_waits", lambda root: True)

    store = adoption_run.AdoptionRunStore(vault)
    positions, continuation = [], None
    for _ in range(10):
        reply = adoption_run.apply(vault, run_id=run_id, plan_id=plan_id, continuation=continuation)
        positions.append(adoption_run._page_position(store.load(run_id)))
        continuation = reply.get("continuation")
        if continuation is None:
            break

    read = [position.get("sources_read") for position in positions]
    # Each call that ends for the writer has read one page more, and the call that has read them all shows none.
    assert len(read) > 5 and read[:-1] == list(range(1, len(read))) and read[-1] is None, read
    assert positions[-1]["phase"] == "applied"
