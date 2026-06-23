"""CLIP image search — ClipIndex store + find() visual-match fusion (model stubbed)."""

from __future__ import annotations

import sqlite3

import numpy as np
import pytest

from kb_mcp import embeddings
from kb_mcp import find as find_module
from kb_mcp import preserve


def _unit(i: int) -> np.ndarray:
    v = np.zeros(embeddings.CLIP_DIM, dtype=np.float32)
    v[i] = 1.0
    return v


def _img(white_rows: int, size: int = 8):
    """An 8x8 grayscale PIL image with `white_rows` white rows — gives _avg_hash
    something structured to differ on (a solid image hashes to all-ones)."""
    from PIL import Image

    arr = np.zeros((size, size), dtype=np.uint8)
    arr[:white_rows, :] = 255
    return Image.fromarray(arr, mode="L")


class _FakeClip:
    """Stub CLIP model: encodes the i-th image to the i-th basis vector."""

    def encode(self, images, convert_to_numpy=True, normalize_embeddings=True):
        return np.stack([_unit(i % embeddings.CLIP_DIM) for i in range(len(images))]).astype(
            np.float32
        )


def test_clip_index_upsert_search_has_delete(vault) -> None:
    idx = embeddings.ClipIndex(vault)
    a_path = "Knowledge Base/Evidence/Yolo/photos/a.jpg"
    b_path = "Knowledge Base/Evidence/Yolo/photos/b.jpg"
    idx.upsert(a_path, _unit(0), 1.0)
    idx.upsert(b_path, _unit(1), 2.0)

    assert idx.has(a_path)
    res = idx.search(_unit(0), k=2)
    assert res[0][0] == a_path  # closest to the query vector
    assert res[0][1] is None    # image rows carry no frame_ts
    assert round(res[0][2], 3) == 1.0

    idx.delete(a_path)
    assert not idx.has(a_path)
    assert idx.search(_unit(0), k=2)[0][0] == b_path


def test_clip_index_empty_search(vault) -> None:
    assert embeddings.ClipIndex(vault).search(_unit(0), k=5) == []


def test_clip_enabled_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KB_MCP_DISABLE_CLIP", "1")
    assert embeddings.clip_enabled() is False
    monkeypatch.delenv("KB_MCP_DISABLE_CLIP", raising=False)
    assert embeddings.clip_enabled() is True


def test_find_clip_surfaces_textless_image(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    """A CLIP visual match surfaces the image sidecar even with ZERO lexical overlap."""
    monkeypatch.delenv("KB_MCP_DISABLE_CLIP", raising=False)
    # Image + sidecar; the text deliberately does NOT contain the query terms.
    res = preserve.preserve_bytes(
        vault, scope="Yolo", category="photos", filename="scene.jpg", data=b"\xff\xd8\xff",
        text="a calm beach at sunset",
    )
    img_vec = _unit(3)
    embeddings.ClipIndex(vault).upsert(res.path, img_vec, 1.0)

    # Stub the heavy models: bge returns empty (no text-vector load), CLIP query → the image vec.
    monkeypatch.setattr(
        embeddings, "embed_texts",
        lambda texts, **kw: np.zeros((len(texts), embeddings.VECTOR_DIM), dtype=np.float32),
    )
    monkeypatch.setattr(embeddings, "embed_clip_text", lambda q: img_vec)

    find_module.clear_cache()
    hits = find_module.find(vault, query="purple dinosaur costume", mode="hybrid")
    match = [h for h in hits if h.path == res.sidecar_path]
    assert match, [h.path for h in hits]
    d = match[0].as_dict()
    assert d["signals"]["clip_rank"] == 1
    assert d["media_type"] == "image"
    assert d["media_file"].endswith("scene.jpg")
    assert "clip_match_at" not in d  # images carry no keyframe timestamp
    assert "clip_frame_ts" not in d["signals"]


def test_has_frames_distinguishes_legacy_single_vector_video(vault) -> None:
    """A video carrying only a legacy single-vector row (frame_ts NULL) must read as
    NOT per-keyframe-indexed, so backfill/worker re-index it instead of skipping."""
    idx = embeddings.ClipIndex(vault)
    img = "Knowledge Base/Evidence/Yolo/photos/a.jpg"
    vid = "Knowledge Base/Evidence/Yolo/vids/legacy.mp4"

    idx.upsert(img, _unit(0), 1.0)            # normal image
    idx.upsert(vid, _unit(1), 1.0)            # STALE single-vector video (frame_ts NULL)

    # has() = any row (true for both); has_frames() = per-keyframe row only.
    assert idx.has(img) and not idx.has_frames(img)
    assert idx.has(vid)                       # the bug: looks "done" to has()
    assert not idx.has_frames(vid)            # the fix: still needs per-keyframe indexing

    idx.upsert_frames(vid, [(10.0, _unit(2)), (20.0, _unit(3))], 2.0)
    assert idx.has_frames(vid)                # now genuinely per-keyframe indexed
    paths, frame_ts, _ = idx.all_vectors()
    assert paths.count(vid) == 2 and None not in [t for p, t in zip(paths, frame_ts) if p == vid]


def test_backfill_reindexes_legacy_single_vector_video(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    """backfill must NOT skip a video that only has a stale single-vector row."""
    monkeypatch.delenv("KB_MCP_DISABLE_CLIP", raising=False)
    from kb_mcp import backfill
    p = vault / "Knowledge Base/Evidence/Old/clips/legacy.mp4"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\x00video")
    rel = "Knowledge Base/Evidence/Old/clips/legacy.mp4"
    embeddings.ClipIndex(vault).upsert(rel, _unit(7), 1.0)  # pre-existing single-vector row
    monkeypatch.setattr(
        embeddings, "embed_video_frames",
        lambda f: [(1.0, _unit(0)), (2.0, _unit(1)), (3.0, _unit(2))],
    )
    stats = backfill.backfill_media(vault, do_ocr=False, log_fn=lambda *a: None)
    assert stats.clip_indexed == 1  # re-indexed, not skipped
    idx = embeddings.ClipIndex(vault)
    assert idx.has_frames(rel)
    paths, _, _ = idx.all_vectors()
    assert paths.count(rel) == 3  # stale NULL row replaced by 3 keyframes


def test_find_clip_skipped_when_disabled(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KB_MCP_DISABLE_CLIP", "1")
    res = preserve.preserve_bytes(
        vault, scope="Yolo", category="photos", filename="scene2.jpg", data=b"\xff\xd8\xff",
        text="a calm beach at sunset",
    )
    embeddings.ClipIndex(vault).upsert(res.path, _unit(3), 1.0)
    monkeypatch.setattr(
        embeddings, "embed_texts",
        lambda texts, **kw: np.zeros((len(texts), embeddings.VECTOR_DIM), dtype=np.float32),
    )
    # If CLIP ran it would raise (no real model) — proving it's gated off, find must not call it.
    monkeypatch.setattr(embeddings, "embed_clip_text", lambda q: (_ for _ in ()).throw(AssertionError("CLIP ran")))
    find_module.clear_cache()
    hits = find_module.find(vault, query="purple dinosaur costume", mode="hybrid")
    assert not [h for h in hits if h.path == res.sidecar_path]


# ---- multi-vector (per-keyframe video) ClipIndex ----------------------------


def test_clip_index_image_upsert_no_duplicate(vault) -> None:
    """Re-upserting the same image replaces its single NULL-frame_ts row (NULL is
    DISTINCT in a SQLite PK, so INSERT OR REPLACE would have duplicated it)."""
    idx = embeddings.ClipIndex(vault)
    a = "Knowledge Base/Evidence/Yolo/photos/a.jpg"
    idx.upsert(a, _unit(0), 1.0)
    idx.upsert(a, _unit(1), 2.0)  # second write must not add a row
    paths, frame_ts, matrix = idx.all_vectors()
    assert paths == [a]
    assert frame_ts == [None]
    assert round(float(matrix[0] @ _unit(1)), 3) == 1.0  # vector was replaced


def test_clip_index_upsert_frames_search_ts_delete(vault) -> None:
    idx = embeddings.ClipIndex(vault)
    v = "Knowledge Base/Evidence/Yolo/vids/m.mp4"
    idx.upsert_frames(v, [(10.0, _unit(0)), (20.0, _unit(1)), (30.0, _unit(2))], 5.0)

    assert idx.has(v)
    paths, _, _ = idx.all_vectors()
    assert paths.count(v) == 3  # one row per keyframe

    best = idx.search(_unit(1), k=10)[0]  # query matches the 20.0s frame
    assert best[0] == v
    assert best[1] == 20.0
    assert round(best[2], 3) == 1.0

    idx.delete(v)
    assert not idx.has(v)
    assert idx.search(_unit(1), k=10) == []


def test_clip_index_upsert_frames_replaces_prior_rows(vault) -> None:
    """upsert_frames clears all prior rows for the file — including a stale
    mean-pooled NULL-ts row from the old single-vector path."""
    idx = embeddings.ClipIndex(vault)
    v = "Knowledge Base/Evidence/Yolo/vids/m.mp4"
    idx.upsert(v, _unit(7), 1.0)  # simulate an old single (NULL-ts) video vector
    idx.upsert_frames(v, [(1.0, _unit(0)), (2.0, _unit(1))], 2.0)
    paths, frame_ts, _ = idx.all_vectors()
    assert paths == [v, v]
    assert sorted(t for t in frame_ts) == [1.0, 2.0]  # no leftover NULL row


def test_clip_index_migration_adds_frame_ts(vault) -> None:
    """Opening a pre-existing OLD-schema .clip.sqlite migrates it in place,
    preserving image rows as frame_ts NULL."""
    p = embeddings.clip_sidecar_path(vault)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.execute(
        "CREATE TABLE images (file_path TEXT PRIMARY KEY, vector BLOB NOT NULL, file_mtime REAL NOT NULL)"
    )
    conn.execute(
        "INSERT INTO images VALUES (?, ?, ?)",
        ("Knowledge Base/Evidence/Yolo/photos/old.jpg", _unit(0).astype(np.float32).tobytes(), 1.0),
    )
    conn.commit()
    conn.close()

    idx = embeddings.ClipIndex(vault)
    paths, frame_ts, matrix = idx.all_vectors()  # triggers _connect → migration
    assert paths == ["Knowledge Base/Evidence/Yolo/photos/old.jpg"]
    assert frame_ts == [None]
    assert round(float(matrix[0] @ _unit(0)), 3) == 1.0

    cols = [r[1] for r in sqlite3.connect(p).execute("PRAGMA table_info(images)").fetchall()]
    assert "frame_ts" in cols


# ---- scene-aware keyframe sampler -------------------------------------------


def test_dedup_keyframes_collapses_static_runs(vault) -> None:
    pytest.importorskip("PIL")  # pHash path needs Pillow (the embeddings extra)
    frames = [(0.0, _img(3)), (1.0, _img(3)), (2.0, _img(6))]  # middle == first
    kept = embeddings._dedup_keyframes(frames)
    assert [ts for ts, _ in kept] == [0.0, 2.0]  # the near-dup at 1.0 is dropped


def test_embed_video_frames_caps_and_aligns(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("PIL")  # builds real images for the pHash/dedup path
    monkeypatch.setenv("KB_MCP_MAX_VIDEO_KEYFRAMES", "3")
    monkeypatch.setattr(embeddings, "get_clip_model", lambda: _FakeClip())
    # 7 visually-distinct candidate frames (1..7 white rows) → none dedup'd → capped to 3.
    candidates = [(float(i * 10), _img(i)) for i in range(1, 8)]
    monkeypatch.setattr(embeddings, "_sample_video_keyframes", lambda path: candidates)

    out = embeddings.embed_video_frames(vault / "fake.mp4")
    assert len(out) == 3  # uniform subsample to the cap
    timestamps = [ts for ts, _ in out]
    assert timestamps == sorted(timestamps)  # time order preserved
    assert all(vec.shape == (embeddings.CLIP_DIM,) for _, vec in out)


def test_embed_video_frames_empty_raises(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(embeddings, "get_clip_model", lambda: _FakeClip())
    monkeypatch.setattr(embeddings, "_sample_video_keyframes", lambda path: [])
    with pytest.raises(embeddings.ClipUnavailable):
        embeddings.embed_video_frames(vault / "fake.mp4")


# ---- find() surfaces the matching video timestamp ---------------------------


def test_find_video_clip_at_timestamp(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    """A text query visually matching ONE keyframe surfaces the video sidecar once,
    with the matching timestamp as `clip_match_at`."""
    monkeypatch.delenv("KB_MCP_DISABLE_CLIP", raising=False)
    res = preserve.preserve_bytes(
        vault, scope="Yolo", category="vids", filename="meeting.mp4", data=b"\x00\x00",
        text="quarterly planning meeting",
    )
    frames = [(0.0, _unit(1)), (42.0, _unit(3)), (120.0, _unit(5))]
    embeddings.ClipIndex(vault).upsert_frames(res.path, frames, 1.0)

    monkeypatch.setattr(
        embeddings, "embed_texts",
        lambda texts, **kw: np.zeros((len(texts), embeddings.VECTOR_DIM), dtype=np.float32),
    )
    monkeypatch.setattr(embeddings, "embed_clip_text", lambda q: _unit(3))  # matches the 42.0s frame

    find_module.clear_cache()
    hits = find_module.find(vault, query="whiteboard diagram", mode="hybrid")
    match = [h for h in hits if h.path == res.sidecar_path]
    assert match, [h.path for h in hits]
    assert len(match) == 1  # N keyframes dedup to ONE video hit
    d = match[0].as_dict()
    assert d["media_type"] == "video"
    assert d["clip_match_at"] == "0:42"
    assert d["signals"]["clip_frame_ts"] == 42.0
