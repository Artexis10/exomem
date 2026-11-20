"""The relayout plan at the size of a large vault, timed against a cold lexical build of the same vault.

This is a drill, not a regression test: it generates a nested vault with a given page count,
so it runs only when `EXOMEM_RELAYOUT_DRILL_PAGES` names that count, and it needs a longer
timeout than the suite's default:

    EXOMEM_RELAYOUT_DRILL_PAGES=20000 uv run pytest -o timeout=3000 -s tests/test_relayout_drill.py

The plan reads every page twice, so its budget is the cost of the other full read of a vault,
the cold lexical build: at most twice that time. It keeps facts, never bodies. A plan that
kept every body would allocate at least the bytes of the bodies on top of its facts, so a peak
below the bodies' bytes shows that it does not, as long as the facts of a page are smaller than its body.
"""

from __future__ import annotations

import json
import os
import resource
import time
import tracemalloc
from pathlib import Path

import pytest
from relayout_fixture import KB

from exomem import lexstore
from exomem.relayout.plan import plan
from exomem.relayout.render import render

PAGES = int(os.environ.get("EXOMEM_RELAYOUT_DRILL_PAGES", "0"))
REPORT = os.environ.get("EXOMEM_RELAYOUT_DRILL_REPORT")
TIME_BUDGET_FACTOR = 2
HOMONYM_GROUP = 25  # pages that share a title, the usual size of a recurring-note family
BIG_GROUP_SHARE = (
    20  # a 1-in-20 share of the pages carry a single title, as a daily-note family does
)
TOPICS = 40  # partition folders below the research area
SENTENCE = "The lighthouse keeper logged the tide against the harbour chart and filed the page. "


def title_of(number: int) -> str:
    if number % BIG_GROUP_SHARE == 0:
        return "Meeting notes"
    if number % 10 == 1:
        return f"Weekly review {number // 10 % (PAGES // (10 * HOMONYM_GROUP) + 1)}"
    return f"Finding {number} about topic {number % TOPICS}"


def folder_of(number: int) -> str:
    return "Notes/Insights" if number % 2 else f"Notes/Research/Topic{number % TOPICS}"


def generate(root: Path, pages: int) -> tuple[int, int, int]:
    """Write `pages` nested pages; return the files, the links and the body bytes written.

    Each page carries four links: to a unique title, by full path to another page, to the
    homonym family of meeting notes, and to a title that no page has.
    """
    (root / KB / "_Schema").mkdir(parents=True)
    (root / KB / "_Schema" / "SKILL.md").write_text("", encoding="utf-8")
    body_bytes = 0
    for number in range(pages):
        other = (number * 7 + 3) % pages
        unique = other
        while not title_of(unique).startswith("Finding"):
            unique = (unique + 1) % pages
        links = (
            f"[[Finding {unique} about topic {unique % TOPICS}]] "
            f"[[{KB}/{folder_of(other)}/p{other:05d}]] [[Meeting notes]] [[No page has this title]]"
        )
        body = f"# Page {number}\n\n{links}\n\n" + SENTENCE * 120
        page = root / KB / folder_of(number) / f"p{number:05d}.md"
        page.parent.mkdir(parents=True, exist_ok=True)
        text = f"---\ntype: {'insight' if number % 2 else 'research-note'}\ntitle: {title_of(number)}\n---\n{body}"
        page.write_text(text, encoding="utf-8")
        body_bytes += len(body.encode("utf-8"))
    return pages + 1, 4 * pages, body_bytes


def test_the_plan_of_a_large_vault_costs_at_most_twice_a_cold_lexical_build(tmp_path, monkeypatch):
    if not PAGES:
        pytest.skip("Explicit drill: set EXOMEM_RELAYOUT_DRILL_PAGES to the page count")
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "state"))
    vault_root = tmp_path / "vault"
    generated_files, generated_links, body_bytes = generate(vault_root, PAGES)

    peak_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    started = time.perf_counter()
    result = plan(vault_root)
    plan_seconds = time.perf_counter() - started
    peak_after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    started = time.perf_counter()
    report = render(result)
    render_seconds = time.perf_counter() - started

    started = time.perf_counter()
    found = lexstore.search_bm25(vault_root, "lighthouse", 3, scope="kb", repair=True)
    lexical_seconds = time.perf_counter() - started

    tracemalloc.start()
    plan(vault_root)
    _current, allocated_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    counts = report["counts"]
    figures = {
        "pages": PAGES,
        "files": counts["files"],
        "links": counts["links"],
        "link_edits": counts["link_edits"],
        "body_bytes": body_bytes,
        "plan_seconds": round(plan_seconds, 2),
        "render_seconds": round(render_seconds, 2),
        "cold_lexical_build_seconds": round(lexical_seconds, 2),
        "plan_allocated_peak_bytes": allocated_peak,
        "process_peak_rss_kib_before_and_after_the_plan": [peak_before, peak_after],
        "stage_seconds": report["diagnostics"]["read"].get("timings_seconds"),
    }
    print("RELAYOUT_DRILL " + json.dumps(figures, sort_keys=True))
    if REPORT:
        Path(REPORT).write_text(json.dumps(figures, indent=1, sort_keys=True), encoding="utf-8")

    assert found, (
        "the cold lexical build must have indexed the vault for the comparison to mean anything"
    )
    assert (counts["files"], counts["links"]) == (generated_files, generated_links)
    assert plan_seconds <= TIME_BUDGET_FACTOR * lexical_seconds
    assert allocated_peak < body_bytes
