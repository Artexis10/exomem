"""Semantic segments — timestamp codec, timed-line parsing, transcript rendering.

Pure logic: no torch, no whisper, no PyAV. The segmenter's embedding calls are
covered separately via the injected embed_fn seam.
"""

from __future__ import annotations

from exomem import find as find_mod
from exomem import semantic_segments as ss

# --- format_ts ----------------------------------------------------------------


def test_format_ts_minutes_and_hours() -> None:
    assert ss.format_ts(5) == "0:05"
    assert ss.format_ts(80) == "1:20"
    assert ss.format_ts(3080) == "51:20"
    assert ss.format_ts(3785) == "1:03:05"
    assert ss.format_ts(3785.4) == "1:03:05"  # rounds like the find formatter


def test_format_ts_matches_find_formatter() -> None:
    for secs in (0, 5, 59.6, 61, 3599, 3600, 3785.4, 7322):
        assert ss.format_ts(secs) == find_mod._format_timestamp(secs)


# --- parse_timed_lines ---------------------------------------------------------


def test_parse_plain_and_diarized_lines() -> None:
    block = "\n".join(
        [
            "[0:05] welcome to the incident review",
            "[51:20] [Alice]: we will start using interaction data",
            "[1:03:05] [Speaker B]: any questions",
        ]
    )
    lines = ss.parse_timed_lines(block)
    assert [round(line.ts, 2) for line in lines] == [5.0, 3080.0, 3785.0]
    assert [line.speaker for line in lines] == [None, "Alice", "Speaker B"]
    assert lines[1].text == "we will start using interaction data"


def test_parse_folds_unmatched_lines_into_previous() -> None:
    block = "\n".join(
        [
            "[0:05] first line",
            "a continuation without a marker",
            "[0:20] second line",
        ]
    )
    lines = ss.parse_timed_lines(block)
    assert len(lines) == 2
    assert lines[0].text == "first line a continuation without a marker"
    assert lines[1].ts == 20.0


def test_parse_skips_leading_unmatched_and_empty() -> None:
    assert ss.parse_timed_lines("no markers here\nat all") == []
    assert ss.parse_timed_lines("") == []


def test_roundtrip_render_then_parse() -> None:
    segs = [(5.0, 9.0, "hello there", None), (3080.0, 3085.0, "topic two", "Alice")]
    block = ss.render_timed_lines(segs)
    lines = ss.parse_timed_lines(block)
    assert [line.speaker for line in lines] == [None, "Alice"]
    assert [line.text for line in lines] == ["hello there", "topic two"]
    assert [line.ts for line in lines] == [5.0, 3080.0]


# --- render_timed_lines ---------------------------------------------------------


def test_render_plain_lines() -> None:
    segs = [(5.2, 9.0, " welcome ", None), (12.0, 15.0, "to the review", None)]
    assert ss.render_timed_lines(segs) == "[0:05] welcome\n[0:12] to the review"


def test_render_diarized_repeats_label_per_segment() -> None:
    segs = [
        (5.0, 9.0, "point one", "Alice"),
        (9.0, 14.0, "point two", "Alice"),
        (14.0, 20.0, "reply", "Speaker B"),
    ]
    out = ss.render_timed_lines(segs)
    assert out.splitlines() == [
        "[0:05] [Alice]: point one",
        "[0:09] [Alice]: point two",
        "[0:14] [Speaker B]: reply",
    ]


def test_render_skips_empty_segments() -> None:
    segs = [(1.0, 2.0, "   ", None), (3.0, 4.0, "kept", None)]
    assert ss.render_timed_lines(segs) == "[0:03] kept"


# --- segmenter (embedding calls faked via the injected embed_fn) ----------------

import numpy as np  # noqa: E402


def _topic_embed(texts: list[str]) -> np.ndarray:
    """Deterministic fake: 'alpha' windows → e0, 'beta' → e1, mixed → normalized blend."""
    out = np.zeros((len(texts), 2), dtype=np.float32)
    for i, t in enumerate(texts):
        a = t.count("alpha")
        b = t.count("beta")
        v = np.array([a + 1e-6, b + 1e-6], dtype=np.float32)
        out[i] = v / np.linalg.norm(v)
    return out


def _timed_block(topic_words: list[str], step: float = 10.0, speakers: list | None = None) -> str:
    lines = []
    for i, w in enumerate(topic_words):
        spk = speakers[i] if speakers else None
        prefix = f"[{ss.format_ts(i * step)}]"
        body = f"[{spk}]: {w} filler words here" if spk else f"{w} filler words here"
        lines.append(f"{prefix} {body}")
    return "\n".join(lines)


def test_two_topics_split_at_the_seam() -> None:
    block = _timed_block(["alpha"] * 8 + ["beta"] * 8)
    segs = ss.segment_transcript(block, embed_fn=_topic_embed)
    assert segs is not None and len(segs) == 2
    assert "alpha" in segs[0] and "beta" not in segs[0]
    assert segs[1].startswith("[1:20]")  # line 8 at 80s opens segment two


def test_uniform_topic_is_one_segment() -> None:
    block = _timed_block(["alpha"] * 12)
    segs = ss.segment_transcript(block, embed_fn=_topic_embed)
    assert segs is not None and len(segs) == 1


def test_too_few_lines_returns_none() -> None:
    block = _timed_block(["alpha"] * 5)
    assert ss.segment_transcript(block, embed_fn=_topic_embed) is None


def test_events_only_when_embed_fails() -> None:
    def boom(texts):
        raise RuntimeError("no model")

    # Speaker change + scene event at the same gap: 0.35 + 0.5 >= 0.55 → boundary.
    speakers = ["A"] * 6 + ["B"] * 6
    block = _timed_block(["alpha"] * 12, speakers=speakers)
    events = ss.Events(scene_ts=[60.0], ocr_ts=[])
    segs = ss.segment_transcript(block, events=events, embed_fn=boom)
    assert segs is not None and len(segs) == 2
    assert segs[1].startswith("[1:00]")


def test_speaker_change_alone_is_not_enough() -> None:
    def boom(texts):
        raise RuntimeError("no model")

    speakers = ["A"] * 6 + ["B"] * 6
    block = _timed_block(["alpha"] * 12, speakers=speakers)
    segs = ss.segment_transcript(block, events=ss.Events([], []), embed_fn=boom)
    assert segs is not None and len(segs) == 1  # 0.35 < 0.55


def test_min_seg_secs_keeps_stronger_boundary() -> None:
    def boom(texts):
        raise RuntimeError("no model")

    # Two candidate gaps 10s apart (< MIN_SEG_SECS): scene+ocr+speaker at 60s
    # (score 1.25) vs scene-only at 70s (0.5 — below threshold anyway); use
    # speaker changes at both to make both viable: A..A|B..B|C..C
    speakers = ["A"] * 6 + ["B"] * 1 + ["C"] * 5
    block = _timed_block(["alpha"] * 12, speakers=speakers)
    events = ss.Events(scene_ts=[60.0, 70.0], ocr_ts=[60.0])
    segs = ss.segment_transcript(block, events=events, embed_fn=boom)
    # gap@60s scores 0.5+0.4+0.35=1.25; gap@70s scores 0.5+0.35=0.85 but is
    # within MIN_SEG_SECS of the stronger accepted boundary → dropped.
    assert segs is not None and len(segs) == 2
    assert segs[1].startswith("[1:00]")


def test_max_words_force_splits() -> None:
    long_words = " ".join(["word"] * 60)
    block = "\n".join(
        f"[{ss.format_ts(i * 40)}] alpha {long_words}" for i in range(10)
    )  # ~610 words per line-pairing, far over the chunk cap
    segs = ss.segment_transcript(block, embed_fn=_topic_embed)
    assert segs is not None and len(segs) >= 2
    from exomem.embeddings import MAX_WORDS_PER_CHUNK

    assert all(len(s.split()) <= MAX_WORDS_PER_CHUNK for s in segs)


def test_segmenter_is_deterministic() -> None:
    block = _timed_block(["alpha"] * 8 + ["beta"] * 8)
    a = ss.segment_transcript(block, embed_fn=_topic_embed)
    b = ss.segment_transcript(block, embed_fn=_topic_embed)
    assert a == b


# --- gather_events on a real (tmp) vault ---------------------------------------


class _FakeImg:
    size = (640, 360)

    def resize(self, size):
        return self

    def convert(self, mode):
        return self

    def save(self, path, format=None, quality=None):
        from pathlib import Path as _P

        _P(path).write_bytes(b"\xff\xd8x")


def test_gather_events_from_frames(vault) -> None:

    from exomem import preserve, scene_frames
    from exomem.embeddings import Scene

    rel = "Knowledge Base/Evidence/Test/clips/demo.mp4"
    video = vault / rel
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"\x00video")
    pairs = scene_frames.write_scene_frames(
        vault,
        video,
        [
            (Scene(0.0, 100.0, 50.0, 0.0), _FakeImg()),
            (Scene(100.0, 200.0, 150.0, 0.6), _FakeImg()),
            (Scene(200.0, 300.0, 250.0, 0.5), _FakeImg()),
        ],
    )
    # OCR: frame 1 and 2 share text (no event); frame 3 differs (event).
    preserve.update_sidecar_extraction(vault, pairs[0][1], text="quarterly revenue chart", engine="tesseract")
    preserve.update_sidecar_extraction(vault, pairs[1][1], text="quarterly revenue chart details", engine="tesseract")
    preserve.update_sidecar_extraction(vault, pairs[2][1], text="incident timeline postmortem", engine="tesseract")
    ev = ss.gather_events(vault, rel)
    assert ev.scene_ts == [100.0, 200.0]  # midpoints between rep timestamps
    assert ev.ocr_ts == [200.0]  # only the low-Jaccard transition


def test_gather_events_missing_frames_dir_is_empty(vault) -> None:
    ev = ss.gather_events(vault, "Knowledge Base/Evidence/nope.mp4")
    assert ev.scene_ts == [] and ev.ocr_ts == []


# --- _chunks_for_page routing seam ----------------------------------------------

from types import SimpleNamespace  # noqa: E402

from exomem import embeddings  # noqa: E402


def _page(title: str, body: str, media_type=None, media_file=None) -> SimpleNamespace:
    return SimpleNamespace(title=title, body=body, media_type=media_type, media_file=media_file)


def test_seam_non_media_page_identical_to_chunk_text(vault, monkeypatch) -> None:
    monkeypatch.setenv("EXOMEM_SEMANTIC_SEGMENTS", "1")
    page = _page("A note", "First paragraph.\n\nSecond paragraph.")
    assert embeddings._chunks_for_page(vault, page) == embeddings.chunk_text(
        page.title, page.body
    )


def test_seam_gate_off_identical_for_timed_media(vault, monkeypatch) -> None:
    monkeypatch.delenv("EXOMEM_SEMANTIC_SEGMENTS", raising=False)
    body = "## Extracted text\n\n" + _timed_block(["alpha"] * 10)
    page = _page("A video", body, media_type="video")
    assert embeddings._chunks_for_page(vault, page) == embeddings.chunk_text(
        page.title, page.body
    )


def test_seam_flat_transcript_falls_back(vault, monkeypatch) -> None:
    monkeypatch.setenv("EXOMEM_SEMANTIC_SEGMENTS", "1")
    body = "## Extracted text\n\njust flat prose with no markers at all"
    page = _page("A video", body, media_type="video")
    assert embeddings._chunks_for_page(vault, page) == embeddings.chunk_text(
        page.title, page.body
    )


def test_seam_timed_video_gets_segment_chunks(vault, monkeypatch) -> None:
    monkeypatch.setenv("EXOMEM_SEMANTIC_SEGMENTS", "1")
    monkeypatch.setattr(
        embeddings, "embed_texts", lambda texts, is_query=False: _topic_embed(texts)
    )
    transcript = _timed_block(["alpha"] * 8 + ["beta"] * 8)
    body = f"# Evidence: demo.mp4\n\nPreserved under Evidence.\n\n## Extracted text\n\n{transcript}\n"
    page = _page("Evidence: demo.mp4", body, media_type="video")
    chunks = embeddings._chunks_for_page(vault, page)
    # Head section chunks first, then one chunk per semantic segment.
    assert any("Preserved under Evidence" in c for c in chunks[:2])
    seg_chunks = [c for c in chunks if "[0:00]" in c or "[1:20]" in c]
    assert len(seg_chunks) == 2
    assert all(c.startswith("Evidence: demo.mp4\n\n") for c in chunks)
    assert "[1:20]" in seg_chunks[1] and "beta" in seg_chunks[1]
