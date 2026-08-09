"""Native renderer grammar + per-fact parity completeness."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from membench.generate import generate_corpus
from membench.native import (
    ParityStatus,
    basic_memory,
    corpus_facts,
    exomem_kb,
    graybox,
    load_corpus_view,
    neutral,
)

T00 = "t00_mini_smoke"


@pytest.fixture(scope="module")
def corpus(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("corpus") / "s1"
    generate_corpus(1, root, template_ids=[T00])
    return root


def test_neutral_represents_everything(corpus: Path) -> None:
    view = load_corpus_view(corpus)
    report = neutral.render(view)
    assert report.missing(corpus_facts(view)) == []
    assert all(e.status is ParityStatus.REPRESENTED for e in report.entries.values())


def test_basic_memory_grammar_and_parity(corpus: Path, tmp_path: Path) -> None:
    view = load_corpus_view(corpus)
    report = basic_memory.render(view, tmp_path / "bm")
    notes = sorted((tmp_path / "bm").glob("*.md"))
    assert notes, "renderer wrote no notes"
    frontmatter = notes[0].read_text(encoding="utf-8")
    assert frontmatter.startswith("---\ntitle: ")
    assert re.search(r"^permalink: [a-z0-9-]+$", frontmatter, flags=re.M)
    observation_lines = [
        line
        for note in notes
        for line in note.read_text(encoding="utf-8").splitlines()
        if line.startswith("- [")
    ]
    assert observation_lines
    assert all(re.fullmatch(r"- \[[a-z]+\] .+ #[a-z0-9-]+", line) for line in observation_lines)
    relation_lines = [
        line
        for note in notes
        for line in note.read_text(encoding="utf-8").splitlines()
        if line.startswith("- relates_to")
    ]
    assert all(re.fullmatch(r"- relates_to \[\[.+\]\]", line) for line in relation_lines)
    assert report.missing(corpus_facts(view)) == []
    unsupported = [e for e in report.entries.values() if e.status is ParityStatus.UNSUPPORTED]
    degraded = [e for e in report.entries.values() if e.status is ParityStatus.DEGRADED]
    assert all(e.reason for e in unsupported + degraded)
    assert any(e.fact_id.startswith("supersedes:") for e in degraded)


def test_exomem_stream_covers_schedule_and_facts(corpus: Path, tmp_path: Path) -> None:
    view = load_corpus_view(corpus)
    report = exomem_kb.render(view, tmp_path / "exo")
    stream = (tmp_path / "exo" / "capture-ops.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(stream) == len(view.sources)
    assert report.missing(corpus_facts(view)) == []


def test_graybox_stream_is_fully_accounted_degraded(corpus: Path, tmp_path: Path) -> None:
    view = load_corpus_view(corpus)
    report = graybox.render(view, tmp_path / "gb")
    assert report.missing(corpus_facts(view)) == []
    assert all(
        e.status is ParityStatus.DEGRADED and e.reason for e in report.entries.values()
    )


def test_basic_memory_keeps_non_latin_entities_distinct(tmp_path: Path) -> None:
    """Non-Latin names must not collapse onto one note or one permalink.

    ``slugify`` drops non-ASCII entirely, so cross-lingual entities would all
    share its fallback stem: notes overwrite each other and the permalink —
    this product's identity key — repeats. That loss is silent (parity reports
    do not cover entity notes), and would score cross-lingual failures as
    contender failures rather than harness ones.
    """

    root = tmp_path / "corpus"
    generate_corpus(1, root, template_ids=["t20_cross_lingual"])
    view = load_corpus_view(root)
    non_latin = [e for e in view.entities if not re.search(r"[a-z0-9]", e.canonical_name.lower())]
    assert len(non_latin) > 1, "expected several non-Latin entities in the corpus"

    out = tmp_path / "bm"
    basic_memory.render(view, out)

    notes = sorted(out.glob("*.md"))
    assert len(notes) == len(view.entities) + len(view.sources)
    permalinks = [
        line.removeprefix("permalink: ").strip()
        for note in notes
        for line in note.read_text(encoding="utf-8").splitlines()
        if line.startswith("permalink: ")
    ]
    assert len(permalinks) == len(set(permalinks)), "permalink collision across notes"
    assert "item" not in permalinks


def test_renderer_refuses_silent_drop(corpus: Path) -> None:
    view = load_corpus_view(corpus)
    report = neutral.render(view)
    with pytest.raises(ValueError, match="duplicate parity entry"):
        report.record(next(iter(report.entries)), ParityStatus.REPRESENTED)
    with pytest.raises(ValueError, match="needs a reason"):
        report.record("fact:new", ParityStatus.UNSUPPORTED)
