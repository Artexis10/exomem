"""find surfaces transcript_match_at for timed media (embeddings disabled —
exercised through the BM25/keyword lanes + the chunk-branch unit seam)."""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import find as find_mod
from exomem import scene_frames
from exomem.embeddings import Scene
from exomem.find import find

VIDEO_REL = "Knowledge Base/Evidence/Test/clips/standup.mp4"


class _FakeImg:
    size = (640, 360)

    def resize(self, size):
        return self

    def convert(self, mode):
        return self

    def save(self, path, format=None, quality=None):
        Path(path).write_bytes(b"\xff\xd8x")


TIMED_TRANSCRIPT = "\n".join(
    [
        "[0:05] good morning everyone welcome to standup",
        "[0:40] first up the deployment pipeline status",
        "[51:20] [Alice]: the flux capacitor migration is unblocked",
        "[52:10] [Bob]: shipping it after lunch then",
    ]
)


def _write_video_sidecar(vault: Path, timed: bool = True) -> Path:
    video = vault / VIDEO_REL
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"\x00video")
    body = TIMED_TRANSCRIPT if timed else TIMED_TRANSCRIPT.replace("[0:05] ", "").replace(
        "[0:40] ", ""
    ).replace("[51:20] ", "").replace("[52:10] ", "")
    sidecar = vault / (VIDEO_REL + ".md")
    sidecar.write_text(
        "---\ntype: source\nsource_type: other\ncaptured: 2026-07-02\n"
        "media_type: video\n"
        f"evidence_file: {VIDEO_REL}\n"
        "extracted_by: faster-whisper:large-v3+timed\ntags: [evidence]\n---\n\n"
        f"# Evidence: standup.mp4\n\n## Extracted text\n\n{body}\n",
        encoding="utf-8",
    )
    return video


def test_hybrid_text_match_carries_transcript_match_at(vault: Path) -> None:
    video = _write_video_sidecar(vault)
    scene_frames.write_scene_frames(
        vault,
        video,
        [
            (Scene(0.0, 60.0, 30.0, 0.0), _FakeImg()),
            (Scene(3000.0, 3200.0, 3100.0, 0.7), _FakeImg()),
        ],
    )
    hits = find(vault, query="flux capacitor migration", mode="hybrid")
    assert len(hits) == 1
    d = hits[0].as_dict()
    assert d["path"] == VIDEO_REL + ".md"
    assert d["transcript_match_at"] == "51:20"
    # Nearest persisted frame (3100s) attached as the visual for the moment.
    assert d["scene_frame"].endswith("t3100000ms.jpg")
    assert d["scene_match_at"] == "51:40"


def test_flat_sidecar_has_no_transcript_field(vault: Path) -> None:
    _write_video_sidecar(vault, timed=False)
    hits = find(vault, query="flux capacitor migration", mode="hybrid")
    assert len(hits) == 1
    assert "transcript_match_at" not in hits[0].as_dict()


def test_keyword_mode_parity(vault: Path) -> None:
    _write_video_sidecar(vault)
    hits = find(vault, query="deployment pipeline", mode="keyword")
    assert len(hits) == 1
    d = hits[0].as_dict()
    assert d["transcript_match_at"] == "0:40"


def test_compact_dict_emits_transcript_match_at(vault: Path) -> None:
    _write_video_sidecar(vault)
    hits = find(vault, query="welcome to standup", mode="hybrid")
    assert hits[0].as_compact_dict()["transcript_match_at"] == "0:05"


def test_chunk_branch_prefers_segment_marker(vault: Path) -> None:
    page = find_mod._CACHE.get(vault / (VIDEO_REL + ".md"), vault)
    if page is None:
        _write_video_sidecar(vault)
        page = find_mod._CACHE.get(vault / (VIDEO_REL + ".md"), vault)
    chunk = "Evidence: standup.mp4\n\n[51:20] [Alice]: the flux capacitor migration is unblocked"
    assert find_mod._transcript_ts_for_hit(page, chunk, "anything") == 3080.0


def test_non_media_page_never_localizes(vault: Path) -> None:
    note = vault / "Knowledge Base/Notes/Insights/timed-looking.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text(
        "---\ntype: insight\nstatus: active\ncreated: 2026-07-02\nupdated: 2026-07-02\n"
        "tags: [x]\n---\n\n# Timed looking\n\n[0:05] this note quotes a transcript line\n",
        encoding="utf-8",
    )
    # scope="kb-only": this test is about a KB note's (non-)localization, not the
    # default scope's auto-widen — which can add an out-of-KB lexical match on a
    # broad query and is exercised elsewhere. kb-only keeps the assertion focused.
    hits = find(vault, query="quotes a transcript", mode="hybrid", scope="kb-only")
    assert len(hits) == 1
    assert "transcript_match_at" not in hits[0].as_dict()


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"], ids=["lf", "crlf", "cr"])
def test_read_at_a_transcript_offset_opens_the_whole_reported_passage(vault: Path, newline: str) -> None:
    from exomem import commands

    path = "Knowledge Base/Sources/workshop.md"
    source = vault / path
    source.parent.mkdir(parents=True, exist_ok=True)
    quote = "Inspect the hinge on the west door."
    body = f"[0:01] We arrived at the shop.\n[0:19] We began the review.\n{quote} {'The discussion continued. ' * 1500}"
    content = f"---\ntitle: Workshop\ntype: source\n---\n\n## Capture\n\n{body}\n"
    source.write_bytes(content.replace("\n", newline).encode())

    hit = next(hit.as_dict() for hit in find(vault, query=quote, mode="keyword") if hit.path == path)
    passage = hit["passage"]
    opened = commands.op_read_memory(vault, path=path, offset=passage["offset"])

    assert quote in passage["text"]
    assert opened["window"]["offset"] == passage["offset"]
    assert opened["body"].startswith(passage["text"])


def test_untyped_source_stays_searchable_without_a_timed_citation(vault: Path) -> None:
    path = "Knowledge Base/Sources/workshop.md"
    source = vault / path
    source.parent.mkdir(parents=True, exist_ok=True)
    quote = "Inspect the hinge on the west door."
    source.write_text(
        f"## Extracted text\n\n[0:01] We arrived at the shop.\n[0:19] {quote}\n",
        encoding="utf-8",
    )

    hit = next(hit.as_dict() for hit in find(vault, query=quote, mode="keyword") if hit.path == path)

    assert quote in hit["excerpt"] and "transcript_match_at" not in hit


def test_a_fenced_extraction_example_does_not_override_summary_rank(vault: Path) -> None:
    source_path = "Knowledge Base/Sources/workshop.md"
    source = vault / source_path
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        "---\ntitle: Workshop\ntype: source\nmedia_type: audio\n---\n\n"
        "```markdown\n## Extracted text\n```\n\n"
        "[0:03] We inspected the gate.\n[0:19] Leave the gate open for the cerulean forklift.\n",
        encoding="utf-8",
    )
    summary_path = "Knowledge Base/Notes/forklift.md"
    summary = vault / summary_path
    summary.parent.mkdir(parents=True, exist_ok=True)
    summary.write_text(
        "---\ntitle: Cerulean forklift\ntype: insight\nstatus: active\nsources:\n  - '[[Sources/workshop]]'\n---\n\n"
        "The cerulean forklift can enter. The cerulean forklift can exit.\n",
        encoding="utf-8",
    )

    hits = find(
        vault, query="cerulean forklift", mode="hybrid", scope="kb-only",
        graph=False, rerank=False, prefer_source_passages=True, limit=3,
    )

    assert [hit.path for hit in hits[:2]] == [summary_path, source_path]
