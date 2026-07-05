"""Local vector embeddings for hybrid search.

Loads `BAAI/bge-base-en-v1.5` lazily (heavy import — torch +
sentence-transformers stays off the keyword-mode hot path). Chunks each
KB page paragraph-wise with title prepended, normalizes vectors so
cosine = dot product, and persists to a per-machine sqlite sidecar at
`<vault>/Knowledge Base/.embeddings.sqlite`.

Sidecar lives outside `_Schema/` deliberately:
- Dotfile → Obsidian Sync ignores it (each machine maintains its own)
- Not bundled in `_Schema.zip` (would inflate every claude.ai schema upload)
- `audit_fix(rebuild_embeddings=True)` rebuilds from the markdown source
  of truth if the sidecar is lost or stale.
"""

from __future__ import annotations

import logging
import math
import os
import random
import sqlite3
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

import numpy as np

from . import accel, vecstore
from .kbdir import kb_dirname

log = logging.getLogger(__name__)


MODEL_NAME = "BAAI/bge-base-en-v1.5"
RERANKER_NAME = "BAAI/bge-reranker-base"
VECTOR_DIM = 768
# bge documentation recommends prefixing queries (not passages) for retrieval.
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "
# Rough word-count cap per chunk. bge-base's tokenizer maxes at 512 tokens;
# words ≈ tokens × 0.75, so 350 words is a safe upstream cap that avoids
# truncation surprises while staying paragraph-coherent.
MAX_WORDS_PER_CHUNK = 350

# CLIP: one shared image+text space, so a text query can match a (textless) photo
# by visual content. An EMBEDDER (measurement) like bge — not a captioning VLM —
# so it stays in-bounds for the pure-substrate server. ViT-B/32 → 512-dim.
CLIP_MODEL_NAME = "clip-ViT-B-32"
CLIP_DIM = 512

_MODEL = None
_MODEL_LOCK = threading.Lock()
_RERANKER = None
_RERANKER_LOCK = threading.Lock()
_CLIP_MODEL = None
_CLIP_LOCK = threading.Lock()
_IMPORT_FAILED = False  # one-time soft-fail flag for upsert_after_write
_CLIP_IMPORT_FAILED = False

# Process-lifetime memo of the per-vault index objects. Sharing ONE instance per
# vault is what makes the in-memory matrix cache survive across find() calls (and
# lets warm-up actually prime it) — previously every call site built a throwaway
# instance whose cache started empty, so all_vectors() paid a full O(vault) reload
# on every find. Keyed by the resolved vault path; guarded for the worker-thread
# pool + file-watcher/media-worker threads that touch these concurrently.
_INDEX_CACHE: dict[str, EmbeddingIndex] = {}
_CLIP_INDEX_CACHE: dict[str, ClipIndex] = {}
_INDEX_CACHE_LOCK = threading.Lock()


def sidecar_path(vault_root: Path) -> Path:
    return vault_root / kb_dirname() / ".embeddings.sqlite"


def clip_sidecar_path(vault_root: Path) -> Path:
    """Separate per-machine sidecar for CLIP image vectors (independent lifecycle)."""
    return vault_root / kb_dirname() / ".clip.sqlite"


def clip_enabled() -> bool:
    """False when EXOMEM_DISABLE_CLIP is set (mirrors EXOMEM_DISABLE_EMBEDDINGS)."""
    return not os.environ.get("EXOMEM_DISABLE_CLIP")


# Which markdown scope the semantic (bge) text index covers.
INDEX_SCOPES = ("kb", "vault")


def index_scope() -> str:
    """Return the semantic-index scope: `"kb"` (default) or `"vault"`.

    `EXOMEM_INDEX_SCOPE=vault` opts the whole Obsidian vault into the vector
    index, so a user's existing notes OUTSIDE `Knowledge Base/` become
    semantically searchable (not keyword-only). Any other/unset value is `"kb"`
    — the historical behavior, which MUST stay byte-identical. The sidecar path
    is unchanged either way (`<vault>/Knowledge Base/.embeddings.sqlite`); only
    the set of files walked into it widens.
    """
    raw = (os.environ.get("EXOMEM_INDEX_SCOPE") or "").strip().lower()
    return "vault" if raw == "vault" else "kb"


def _index_walk(vault_root: Path):
    """Yield the markdown paths the semantic index should cover, per `index_scope`.

    - `"kb"`   → `find._walk_md(<vault>/Knowledge Base)` — byte-identical to the
      historical KB-only walk (empty when there is no `Knowledge Base/`).
    - `"vault"`→ `vault.walk_vault_md(<vault>)` — the whole vault.

    Only the WALK differs by scope; every caller still applies the shared
    `_is_embeddable_path` + `access.is_indexable` + `_chunks_for_page` filtering,
    so the chunk/embed path is identical for a KB file whichever scope is active.
    """
    from . import find as find_module

    if index_scope() == "vault":
        from .vault import walk_vault_md

        yield from walk_vault_md(vault_root)
    else:
        kb = vault_root / kb_dirname()
        if kb.is_dir():
            yield from find_module._walk_md(kb)


# Navigation files that aren't real content — their bodies are
# auto-generated summaries / activity feeds and would just add noise to
# vector search ("find recent activity" should surface the activity, not
# log.md itself).
_SKIP_NAMES = frozenset({"log.md", "index.md"})


def _is_embeddable_path(path: Path) -> bool:
    if path.suffix.lower() != ".md":
        return False
    return path.name.lower() not in _SKIP_NAMES


def get_model():
    """Lazy singleton. Device via `accel.select_device` (CUDA > MPS > CPU)."""
    global _MODEL
    if _MODEL is not None:
        return _MODEL
    with _MODEL_LOCK:
        if _MODEL is not None:
            return _MODEL
        # Heavy import stays local — keyword-mode and existing tests must not
        # pay this cost.
        from sentence_transformers import SentenceTransformer

        device = accel.select_device()
        log.info("loading embedding model %s on %s", MODEL_NAME, device)
        _MODEL = _maybe_half(SentenceTransformer(MODEL_NAME, device=device), device)
    return _MODEL


def get_reranker():
    """Lazy singleton for the cross-encoder reranker. Device via `accel.select_device`."""
    global _RERANKER
    if _RERANKER is not None:
        return _RERANKER
    with _RERANKER_LOCK:
        if _RERANKER is not None:
            return _RERANKER
        from sentence_transformers import CrossEncoder

        device = accel.select_device()
        log.info("loading reranker %s on %s", RERANKER_NAME, device)
        _RERANKER = CrossEncoder(RERANKER_NAME, device=device)
    return _RERANKER


def _maybe_half(model, device: str):
    """Run bge/CLIP in fp16 on Apple Silicon (MPS): ~half the memory and faster encodes,
    and these retrieval models tolerate half precision well. Gated to MPS only — CPU fp16
    is emulated (slower) and the CUDA path stays fp32 for cross-run/voiceprint parity.
    Disable with EXOMEM_MPS_FP16=0.

    Storage is unaffected: every vector is upcast to float32 before it hits the sqlite blob
    (the astype(np.float32) guards in embed_*/upsert_*), so the on-disk format is identical —
    only the computed precision changes. Existing fp32 vectors differ from new fp16 ones by
    ~1e-3 (harmless for ranking); `audit_fix(rebuild_embeddings=True)` re-embeds for exact
    consistency if wanted."""
    if device != "mps" or os.environ.get("EXOMEM_MPS_FP16", "1") == "0":
        return model
    try:
        return model.half()
    except Exception:  # noqa: BLE001 — a precision tweak must never break model load
        log.warning("fp16 (MPS) conversion failed; staying fp32", exc_info=True)
        return model


class ClipUnavailable(Exception):
    """CLIP (sentence-transformers/Pillow) isn't importable — soft-fail signal."""


def _clip_device() -> str:
    """Device for CLIP. Honors EXOMEM_CLIP_DEVICE; otherwise CUDA > MPS > CPU via
    `accel.select_device`, but avoids CUDA when ASR is active in this process.

    Why avoid CUDA under ASR: faster-whisper's CUDA-12 cuDNN/cuBLAS wheels get
    PATH-prepended (extract._ensure_cuda_dll_path) so ctranslate2 can load — which
    then shadows torch-cu132's bundled cuDNN 13 and makes CLIP's ViT Conv2d die with
    CUDNN_STATUS_SUBLIBRARY_VERSION_MISMATCH. bge survives (pure transformer, no conv);
    CLIP's vision tower doesn't. Since the media worker prewarms ASR at startup, having
    extraction enabled means PATH is already poisoned, so CLIP falls to CPU there — a
    tiny ViT-B/32, off the request path, embeds in well under a second.

    This clash is **CUDA-only**: `avoid_cuda_when_asr` fires only when the auto-pick
    would be CUDA, so on Apple Silicon CLIP keeps the MPS (Metal) GPU even with ASR
    running. A CLIP-only box (EXOMEM_DISABLE_MEDIA_EXTRACTION set) also keeps the GPU.
    """
    return accel.select_device(override_env="EXOMEM_CLIP_DEVICE", avoid_cuda_when_asr=True)


def get_clip_model():
    """Lazy CLIP singleton (encodes BOTH images and text). Device via _clip_device()."""
    global _CLIP_MODEL
    if _CLIP_MODEL is not None:
        return _CLIP_MODEL
    with _CLIP_LOCK:
        if _CLIP_MODEL is not None:
            return _CLIP_MODEL
        from sentence_transformers import SentenceTransformer

        device = _clip_device()
        log.info("loading CLIP model %s on %s", CLIP_MODEL_NAME, device)
        _CLIP_MODEL = _maybe_half(SentenceTransformer(CLIP_MODEL_NAME, device=device), device)
    return _CLIP_MODEL


def embed_image(path: Path) -> np.ndarray:
    """Encode an image file → float32 (512,), L2-normalized for cosine.

    Raises ClipUnavailable when CLIP/Pillow aren't installed so callers can soft-skip.
    """
    try:
        from PIL import Image
    except ImportError as e:
        raise ClipUnavailable(f"Pillow not installed: {e}") from e
    try:
        model = get_clip_model()
    except ImportError as e:
        raise ClipUnavailable(f"sentence-transformers not installed: {e}") from e
    with Image.open(path) as img:
        vec = model.encode(img.convert("RGB"), convert_to_numpy=True, normalize_embeddings=True)
    return vec.astype(np.float32, copy=False)


def embed_clip_text(query: str) -> np.ndarray:
    """Encode a text query into CLIP space → float32 (512,), L2-normalized."""
    try:
        model = get_clip_model()
    except ImportError as e:
        raise ClipUnavailable(f"sentence-transformers not installed: {e}") from e
    vec = model.encode([query], convert_to_numpy=True, normalize_embeddings=True)[0]
    return vec.astype(np.float32, copy=False)


def embed_clip_texts(texts: list[str]) -> np.ndarray:
    """Batch-encode texts into CLIP space → float32 `(N, 512)`, L2-normalized.

    Same shared image+text space as `embed_image`, so a cosine between an image vector
    and these text vectors is a valid zero-shot match score. Used to embed the fixed
    image-tag vocabulary once (image_tags). Raises ClipUnavailable when CLIP is missing.
    """
    if not texts:
        return np.zeros((0, CLIP_DIM), dtype=np.float32)
    try:
        model = get_clip_model()
    except ImportError as e:
        raise ClipUnavailable(f"sentence-transformers not installed: {e}") from e
    vecs = model.encode(texts, convert_to_numpy=True, normalize_embeddings=True)
    return vecs.astype(np.float32, copy=False)


CLIP_VIDEO_FRAMES = 8  # frame budget for the unknown-duration sequential fallback


# --- Per-keyframe multi-vector video sampling -------------------------------
# One mean-pooled vector blurs a long/multi-scene video. Instead we keep N
# per-keyframe vectors so a video is findable at the SPECIFIC moment. The
# sampler is duration-scaled seek-sampling (O(1) in length, no full decode) +
# perceptual-hash near-dup suppression (collapses static talking-head runs),
# capped to bound storage. No new dependency — PIL + numpy only.
MAX_VIDEO_KEYFRAMES = 40  # hard cap on vectors per video (EXOMEM_MAX_VIDEO_KEYFRAMES overrides)
MIN_VIDEO_KEYFRAMES = 4
VIDEO_CANDIDATE_INTERVAL_SECS = 8  # ≈ one candidate keyframe per this many seconds
PHASH_DEDUP_DISTANCE = 5  # Hamming distance under which two frames count as near-dups


def _max_video_keyframes() -> int:
    raw = os.environ.get("EXOMEM_MAX_VIDEO_KEYFRAMES")
    if raw:
        try:
            v = int(raw)
            if v > 0:
                return v
        except ValueError:
            pass
    return MAX_VIDEO_KEYFRAMES


def _hash_bits(arr: np.ndarray) -> int:
    """Pack a small grayscale array into an average-hash int: bit per pixel vs mean.

    Shared core between the PIL path (`_avg_hash`) and the ndarray path used by the
    scene-detection metrics pass, so both produce comparable hashes.
    """
    flat = np.asarray(arr, dtype=np.float32).ravel()
    bits = flat >= flat.mean()
    val = 0
    for b in bits:
        val = (val << 1) | int(b)
    return val


def _avg_hash(img, *, size: int = 8) -> int:
    """64-bit perceptual average-hash: downscale → grayscale → bit per pixel vs mean.

    Keys on luminance *structure*, so it distinguishes textured frames (faces, slides,
    whiteboards — what real recordings contain) but is blind to two flat frames of
    differing uniform colour (both hash to all-ones). Dedup is best-effort; a
    pathologically flat video simply keeps fewer keyframes.
    """
    small = img.convert("L").resize((size, size))
    return _hash_bits(np.asarray(small, dtype=np.float32))


def _pool_gray(arr: np.ndarray, *, size: int = 8) -> np.ndarray:
    """Mean-pool a grayscale array down to size×size (hash input for ndarray frames)."""
    a = np.asarray(arr, dtype=np.float32)
    h, w = a.shape
    return a[: h - h % size, : w - w % size].reshape(
        size, h // size, size, w // size
    ).mean(axis=(1, 3))


def _gray_hist(arr: np.ndarray, *, bins: int = 32) -> np.ndarray:
    """Normalized grayscale histogram (sums to 1) — a global-luminance signature that
    catches shifts the structural average-hash is blind to (two flat frames of
    differing brightness)."""
    h, _ = np.histogram(np.asarray(arr).ravel(), bins=bins, range=(0, 256))
    total = h.sum()
    if not total:
        return np.zeros(bins, dtype=np.float32)
    return (h / total).astype(np.float32)


def _hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def _sample_video_keyframes(path: Path) -> list[tuple[float, object]]:
    """Seek-sample duration-scaled candidate keyframes → `(timestamp_seconds, PIL image)`.

    Candidate count scales with duration (≈ one per `VIDEO_CANDIDATE_INTERVAL_SECS`),
    clamped to `[MIN_VIDEO_KEYFRAMES, 2×cap]` so a fast-cut video has headroom for the
    pHash dedup to keep distinct scenes. Seeks to each timestamp (O(1) in length);
    falls back to first-N sequential decode when the duration is unknown.
    """
    try:
        import av
    except ImportError as e:
        raise ClipUnavailable(f"PyAV not installed: {e}") from e
    cap = _max_video_keyframes()
    frames: list[tuple[float, object]] = []
    with av.open(str(path)) as container:
        if not container.streams.video:
            return frames
        stream = container.streams.video[0]
        stream.thread_type = "AUTO"
        total_secs = 0.0
        if stream.duration and stream.time_base:
            total_secs = float(stream.duration * stream.time_base)
        elif container.duration:
            total_secs = container.duration / av.time_base
        if total_secs > 0 and stream.time_base:
            n = max(MIN_VIDEO_KEYFRAMES,
                    min(math.ceil(total_secs / VIDEO_CANDIDATE_INTERVAL_SECS), 2 * cap))
            for k in range(n):
                t = total_secs * (k + 0.5) / n  # evenly-spaced midpoints
                try:
                    container.seek(int(t / float(stream.time_base)), stream=stream, backward=True)
                    frames.append((t, next(container.decode(stream)).to_image()))
                except Exception:  # noqa: BLE001 — best-effort sample; skip a bad seek
                    continue
        if not frames:  # unknown duration or all seeks failed → first-N sequential
            container.seek(0)
            fallback = max(MIN_VIDEO_KEYFRAMES, min(CLIP_VIDEO_FRAMES, cap))
            for i, frame in enumerate(container.decode(stream)):
                ts = float(frame.time) if frame.time is not None else float(i)
                frames.append((ts, frame.to_image()))
                if len(frames) >= fallback:
                    break
    return frames


def _dedup_keyframes(
    frames: list[tuple[float, object]], *, distance: int = PHASH_DEDUP_DISTANCE
) -> list[tuple[float, object]]:
    """Drop frames whose average-hash is within `distance` of the last KEPT frame —
    collapses static runs while keeping scene changes. Soft: returns input on any error."""
    if len(frames) <= 1:
        return frames
    try:
        kept = [frames[0]]
        last_hash = _avg_hash(frames[0][1])
        for ts, img in frames[1:]:
            h = _avg_hash(img)
            if _hamming(h, last_hash) <= distance:
                continue
            kept.append((ts, img))
            last_hash = h
        return kept
    except Exception:  # noqa: BLE001 — dedup is a best-effort optimisation
        return frames


# --- Visual-change scene detection (EXOMEM_VIDEO_SCENE_FRAMES) --------------
# Boundary thresholds sit deliberately ABOVE the near-dup band: dedup collapses
# frames within PHASH_DEDUP_DISTANCE (5), a scene boundary needs a hash change
# > SCENE_HASH_THRESHOLD (10) — a hysteresis gap so jitter never becomes a scene.
SCENE_HASH_THRESHOLD = 10  # hamming bits (of 64); EXOMEM_VIDEO_SCENE_THRESHOLD overrides
SCENE_HIST_THRESHOLD = 0.35  # L1 distance between normalized 32-bin histograms (max 2.0)
SCENE_MIN_SECS = 4.0  # boundaries closer than this merge; EXOMEM_VIDEO_SCENE_MIN_SECS overrides


def scene_frames_enabled() -> bool:
    """EXOMEM_VIDEO_SCENE_FRAMES gates scene detection + persisted scene frames.

    Default OFF: video keyframe selection stays the uniform sampler and no frame
    files are written — byte-identical to the pre-feature behavior.
    """
    return bool(os.environ.get("EXOMEM_VIDEO_SCENE_FRAMES"))


def _scene_hash_threshold() -> int:
    raw = os.environ.get("EXOMEM_VIDEO_SCENE_THRESHOLD")
    if raw:
        try:
            v = int(raw)
            if 0 < v <= 64:
                return v
        except ValueError:
            pass
    return SCENE_HASH_THRESHOLD


def _scene_min_secs() -> float:
    raw = os.environ.get("EXOMEM_VIDEO_SCENE_MIN_SECS")
    if raw:
        try:
            v = float(raw)
            if v >= 0:
                return v
        except ValueError:
            pass
    return SCENE_MIN_SECS


@dataclass(frozen=True)
class Scene:
    """One detected scene: `[start_ts, end_ts]` with a representative timestamp.

    `boundary_score` is the normalized change score of the boundary that OPENED the
    scene (0.0 for the first scene, which has no opening boundary) — used to merge
    the weakest boundaries first when detection exceeds the keyframe cap.
    """

    start_ts: float
    end_ts: float
    rep_ts: float
    boundary_score: float


def detect_scenes(
    series: list[tuple[float, int, np.ndarray]],
    *,
    hash_threshold: int | None = None,
    hist_threshold: float | None = None,
    min_scene_secs: float | None = None,
    max_scenes: int | None = None,
) -> list[Scene]:
    """Pure scene-boundary detection over `[(ts, hash64, hist)]` candidate metrics.

    Anchor-based (the `_dedup_keyframes` pattern generalized): each candidate is
    compared to the CURRENT scene's anchor, not the previous frame, so slow drift
    doesn't fragment a scene and static runs collapse. A boundary opens when the
    hash hamming distance exceeds `hash_threshold` OR the histogram L1 distance
    exceeds `hist_threshold`. A boundary within `min_scene_secs` of the previous
    one merges into it (the anchor re-points at the newest content, so A→B→A
    flicker and fades yield one boundary). When more than `max_scenes` result, the
    adjacent pair with the weakest opening boundary merges first. Representative
    timestamp = candidate nearest the scene's temporal midpoint.
    """
    if not series:
        return []
    if hash_threshold is None:
        hash_threshold = _scene_hash_threshold()
    if hist_threshold is None:
        hist_threshold = SCENE_HIST_THRESHOLD
    if min_scene_secs is None:
        min_scene_secs = _scene_min_secs()
    if max_scenes is None:
        max_scenes = _max_video_keyframes()

    ts0, h0, hist0 = series[0]
    # Working form: candidate timestamps + the anchor metrics + opening-boundary score.
    scenes: list[dict] = [
        {"ts": [ts0], "anchor": (h0, np.asarray(hist0, dtype=np.float32)), "score": math.inf}
    ]
    last_boundary_ts = ts0
    for ts, h, hist in series[1:]:
        hist = np.asarray(hist, dtype=np.float32)
        anchor_h, anchor_hist = scenes[-1]["anchor"]
        d_hash = _hamming(h, anchor_h)
        d_hist = float(np.abs(hist - anchor_hist).sum())
        if d_hash <= hash_threshold and d_hist <= hist_threshold:
            scenes[-1]["ts"].append(ts)
            continue
        score = max(d_hash / 64.0, d_hist / 2.0)
        if ts - last_boundary_ts < min_scene_secs:
            # Same transition (flicker/fade): absorb, re-anchor to the newest content.
            scenes[-1]["ts"].append(ts)
            scenes[-1]["anchor"] = (h, hist)
            scenes[-1]["score"] = max(scenes[-1]["score"], score)
            last_boundary_ts = ts
            continue
        scenes.append({"ts": [ts], "anchor": (h, hist), "score": score})
        last_boundary_ts = ts

    # Over the cap: merge the scene whose OPENING boundary is weakest into its
    # predecessor — keeps the strongest boundaries (better than uniform subsampling).
    while len(scenes) > max(1, max_scenes) and len(scenes) > 1:
        i = min(range(1, len(scenes)), key=lambda k: scenes[k]["score"])
        scenes[i - 1]["ts"].extend(scenes[i]["ts"])
        del scenes[i]

    out: list[Scene] = []
    for sc in scenes:
        ts_list = sc["ts"]
        start, end = ts_list[0], ts_list[-1]
        mid = (start + end) / 2.0
        rep = min(ts_list, key=lambda t: abs(t - mid))
        score = 0.0 if sc["score"] is math.inf else float(sc["score"])
        out.append(Scene(start_ts=start, end_ts=end, rep_ts=rep, boundary_score=score))
    return out


SCENE_CANDIDATE_MIN_GAP_SECS = 2.0  # pass-1 thinning: at most one candidate per this
SCENE_CANDIDATE_CAP = 900  # hard bound on pass-1 candidates (all-intra screen captures)


def _iter_iframe_metrics(path: Path) -> list[tuple[float, int, np.ndarray]]:
    """Pass 1 of scene detection: I-frame-only metrics scan → `[(ts, hash64, hist)]`.

    Decodes ONLY keyframes (`skip_frame NONKEY`), reformatted libav-side to 64×64
    grayscale — no full-res frame is ever materialized here. Encoder scenecut logic
    already concentrates I-frames at hard cuts, so candidate density adapts to the
    content. Thinned to one candidate per `SCENE_CANDIDATE_MIN_GAP_SECS`, with the
    gap widened so a pathological all-intra stream stays under `SCENE_CANDIDATE_CAP`.
    """
    try:
        import av
    except ImportError as e:
        raise ClipUnavailable(f"PyAV not installed: {e}") from e
    out: list[tuple[float, int, np.ndarray]] = []
    with av.open(str(path)) as container:
        if not container.streams.video:
            return out
        stream = container.streams.video[0]
        stream.thread_type = "AUTO"
        stream.codec_context.skip_frame = "NONKEY"
        total_secs = 0.0
        if stream.duration and stream.time_base:
            total_secs = float(stream.duration * stream.time_base)
        elif container.duration:
            total_secs = container.duration / av.time_base
        min_gap = SCENE_CANDIDATE_MIN_GAP_SECS
        if total_secs:
            min_gap = max(min_gap, total_secs / SCENE_CANDIDATE_CAP)
        last_ts: float | None = None
        for frame in container.decode(stream):
            if frame.time is None:
                continue
            ts = float(frame.time)
            if last_ts is not None and ts - last_ts < min_gap:
                continue
            gray = frame.reformat(width=64, height=64, format="gray8").to_ndarray()
            out.append((ts, _hash_bits(_pool_gray(gray)), _gray_hist(gray)))
            last_ts = ts
            if len(out) >= SCENE_CANDIDATE_CAP:
                break
    return out


def _decode_frames_at(path: Path, ts_list: list[float]) -> list[object]:
    """Pass 2 of scene detection: seek+decode ONE full-res frame per timestamp.

    Same O(1) seek pattern as `_sample_video_keyframes`. Returns one entry per
    requested timestamp; a failed seek/decode yields None at that position.
    """
    try:
        import av
    except ImportError as e:
        raise ClipUnavailable(f"PyAV not installed: {e}") from e
    images: list[object] = []
    with av.open(str(path)) as container:
        if not container.streams.video:
            return [None] * len(ts_list)
        stream = container.streams.video[0]
        stream.thread_type = "AUTO"
        for t in ts_list:
            try:
                container.seek(int(t / float(stream.time_base)), stream=stream, backward=True)
                images.append(next(container.decode(stream)).to_image())
            except Exception:  # noqa: BLE001 — best-effort; a bad seek skips one frame
                images.append(None)
    return images


def _metrics_from_frames(frames: list[tuple[float, object]]) -> list[tuple[float, int, np.ndarray]]:
    """Scene metrics for already-decoded PIL frames (the uniform-sampler fallback)."""
    out: list[tuple[float, int, np.ndarray]] = []
    for ts, img in frames:
        arr = np.asarray(img.convert("L").resize((64, 64)), dtype=np.uint8)
        out.append((ts, _hash_bits(_pool_gray(arr)), _gray_hist(arr)))
    return out


def sample_video_scenes(path: Path) -> list[tuple[Scene, object]]:
    """Scene-aware sampling → `[(Scene, full-res PIL representative frame)]`.

    Cheap I-frame metrics pass → `detect_scenes` → decode only the winners. Falls
    back to the uniform seek-sampler's frames (metrics computed from the already-
    decoded images — no second decode) when the I-frame pass fails or yields fewer
    than `MIN_VIDEO_KEYFRAMES` candidates (unknown duration, sparse-keyframe encodes).
    """
    candidates: list[tuple[float, int, np.ndarray]] = []
    try:
        candidates = _iter_iframe_metrics(path)
    except ClipUnavailable:
        raise
    except Exception as e:  # noqa: BLE001 — pass 1 is an optimisation; fall back
        log.warning("I-frame metrics pass failed for %s: %s; falling back", path.name, e)
        candidates = []
    if len(candidates) >= MIN_VIDEO_KEYFRAMES:
        scenes = detect_scenes(candidates)
        images = _decode_frames_at(path, [s.rep_ts for s in scenes])
        pairs = [(s, img) for s, img in zip(scenes, images, strict=True) if img is not None]
        if pairs:
            return pairs
    frames = _sample_video_keyframes(path)
    if not frames:
        return []
    scenes = detect_scenes(_metrics_from_frames(frames))
    by_ts = dict(frames)
    return [(s, by_ts[s.rep_ts]) for s in scenes if s.rep_ts in by_ts]


def embed_video_scenes(
    path: Path,
) -> tuple[list[tuple[float, np.ndarray]], list[tuple[Scene, object]]]:
    """Scene-aware variant of `embed_video_frames`: ONE decode pass yields both the
    per-scene CLIP vectors (at representative timestamps) and the full-res
    representative images for persistence. Raises ClipUnavailable like its sibling.
    """
    try:
        from PIL import Image  # noqa: F401 — decoded frames are PIL images
    except ImportError as e:
        raise ClipUnavailable(f"Pillow not installed: {e}") from e
    try:
        model = get_clip_model()
    except ImportError as e:
        raise ClipUnavailable(f"sentence-transformers not installed: {e}") from e
    pairs = sample_video_scenes(path)
    if not pairs:
        raise ClipUnavailable(f"no decodable video frames in {path.name}")
    vecs = model.encode(
        [img for _, img in pairs], convert_to_numpy=True, normalize_embeddings=True
    )
    vectors = [
        (float(s.rep_ts), vecs[i].astype(np.float32, copy=False))
        for i, (s, _) in enumerate(pairs)
    ]
    return vectors, pairs


def embed_video_frames(path: Path) -> list[tuple[float, np.ndarray]]:
    """Encode a video → `[(timestamp_seconds, CLIP vector)]`, one per keyframe.

    Multi-vector replacement for `embed_video`'s single mean-pooled vector: scene-aware
    (duration-scaled seek-sampling + perceptual-hash near-dup suppression, capped at
    `MAX_VIDEO_KEYFRAMES`) so a long/multi-scene video is findable at the SPECIFIC moment.
    Each vector is 512-d, L2-normalized. Raises ClipUnavailable if CLIP/PyAV/Pillow are
    missing or no frame decodes.

    With `EXOMEM_VIDEO_SCENE_FRAMES` set, keyframes are chosen by visual-change scene
    detection (`embed_video_scenes`) instead of the uniform sampler; unset keeps this
    path byte-identical to the pre-feature behavior.
    """
    if scene_frames_enabled():
        vectors, _ = embed_video_scenes(path)
        return vectors
    try:
        from PIL import Image  # noqa: F401 — frame.to_image() returns a PIL image
    except ImportError as e:
        raise ClipUnavailable(f"Pillow not installed: {e}") from e
    try:
        model = get_clip_model()
    except ImportError as e:
        raise ClipUnavailable(f"sentence-transformers not installed: {e}") from e
    candidates = _sample_video_keyframes(path)
    if not candidates:
        raise ClipUnavailable(f"no decodable video frames in {path.name}")
    kept = _dedup_keyframes(candidates)
    cap = _max_video_keyframes()
    if len(kept) > cap:  # uniform subsample preserving time order
        idx = sorted(set(np.linspace(0, len(kept) - 1, cap).round().astype(int).tolist()))
        kept = [kept[i] for i in idx]
    images = [img for _, img in kept]
    vecs = model.encode(images, convert_to_numpy=True, normalize_embeddings=True)
    return [(float(ts), vecs[i].astype(np.float32, copy=False)) for i, (ts, _) in enumerate(kept)]


def rerank_pairs(query: str, passages: list[str]) -> np.ndarray:
    """Score `(query, passage)` pairs with bge-reranker-base. Returns float32 (N,).

    Higher = more relevant. Scores are not bounded to [0, 1] — they're the
    CrossEncoder's raw logits, useful for relative ordering only.
    """
    if not passages:
        return np.zeros(0, dtype=np.float32)
    model = get_reranker()
    pairs = [(query, p) for p in passages]
    scores = model.predict(
        pairs,
        batch_size=32,
        convert_to_numpy=True,
        show_progress_bar=False,
    )
    return scores.astype(np.float32, copy=False)


def chunk_text(title: str, body: str) -> list[str]:
    """Paragraph-split body with title prepended for retrieval context.

    - Split on blank-line paragraph boundaries.
    - Drop empty/whitespace-only chunks.
    - Truncate overlong chunks at word boundary so the tokenizer doesn't lop.
    - Always prepend `<title>\\n\\n` so embeddings of orphan paragraphs still
      carry the document's topic.
    """
    title = (title or "").strip()
    body = (body or "").strip()
    if not body:
        return [title] if title else []
    paragraphs = [p.strip() for p in body.split("\n\n") if p.strip()]
    out: list[str] = []
    for p in paragraphs:
        words = p.split()
        if len(words) > MAX_WORDS_PER_CHUNK:
            p = " ".join(words[:MAX_WORDS_PER_CHUNK])
        chunk = f"{title}\n\n{p}" if title else p
        out.append(chunk)
    return out


def _chunks_for_page(vault_root: Path, page) -> list[str]:
    """Chunking router — the single seam every writer/rebuild path goes through.

    Gated (`EXOMEM_SEMANTIC_SEGMENTS`) audio/video sidecars whose transcript is
    timed get SEMANTIC SEGMENT chunks (each starting with its `[timestamp]`
    marker — what `find` surfaces as `transcript_match_at`), with the sections
    before/after `## Extracted text` still paragraph-chunked in document order.
    Every other page — and the gate-off world — returns `chunk_text` output
    unchanged (equality-tested).
    """
    from . import semantic_segments as ss

    if not (
        ss.semantic_segments_enabled()
        and getattr(page, "media_type", None) in ("audio", "video")
    ):
        return chunk_text(page.title, page.body)
    body = page.body or ""
    idx = body.find("## Extracted text")
    if idx == -1:
        return chunk_text(page.title, page.body)
    head = body[:idx]
    rest = body[idx + len("## Extracted text"):]
    nxt = rest.find("\n## ")
    transcript = (rest if nxt == -1 else rest[:nxt]).strip()
    tail = "" if nxt == -1 else rest[nxt + 1 :]
    timed_lines = sum(1 for line in transcript.splitlines() if ss.TIMED_LINE_RE.match(line))
    if timed_lines < ss.MIN_TIMED_LINES:
        return chunk_text(page.title, page.body)
    events = (
        ss.gather_events(vault_root, page.media_file)
        if getattr(page, "media_file", None)
        else ss.Events([], [])
    )
    segs = ss.segment_transcript(transcript, events=events)
    if segs is None:
        return chunk_text(page.title, page.body)
    title = (page.title or "").strip()
    out: list[str] = []
    if head.strip():
        out.extend(chunk_text(title, head))
    out.extend(f"{title}\n\n{s}" if title else s for s in segs)
    if tail.strip():
        out.extend(chunk_text(title, tail))
    return out


def embed_texts(texts: list[str], *, is_query: bool = False) -> np.ndarray:
    """Batch-encode texts → float32 `(N, 768)`, L2-normalized for cosine."""
    if not texts:
        return np.zeros((0, VECTOR_DIM), dtype=np.float32)
    model = get_model()
    if is_query:
        texts = [QUERY_PREFIX + t for t in texts]
    vecs = model.encode(
        texts,
        batch_size=32,
        convert_to_numpy=True,
        normalize_embeddings=True,
        show_progress_bar=False,
    )
    return vecs.astype(np.float32, copy=False)


def _apply_sidecar_pragmas(conn: sqlite3.Connection) -> None:
    """WAL + relaxed sync on a sidecar connection.

    WAL lets a find (reader) proceed WITHOUT blocking a concurrent backfill
    (writer) and vice-versa — the whole point of this change: find latency stops
    tracking sidecar write churn. `synchronous=NORMAL` collapses the per-write
    fsync cost (the 37-42s note-writes were writer↔writer fsync contention).
    Safe here because the sidecar is a LOCAL, per-machine dotfile Obsidian Sync
    ignores (WAL's shared-memory requirement rules out network filesystems, which
    this sidecar is designed never to live on). The `-wal`/`-shm` siblings inherit
    the leading dot, so Sync ignores them too. SQLite's automatic checkpoint
    (wal_autocheckpoint, ~1000 pages) bounds the WAL under a long backfill.
    """
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
    except sqlite3.Error as e:  # pragma: no cover — WAL unavailable (e.g. odd FS)
        log.warning("sidecar WAL pragmas failed (%s); continuing on default journal", e)


def _file_block(keys: list[str], rel_path: str) -> tuple[int, int]:
    """Locate `rel_path`'s rows in a `(file_path, …)`-sorted key list.

    Returns `(lo, hi)` for the contiguous block of rows whose file_path equals
    `rel_path` (rows for one file are always contiguous under the load ordering).
    When the file is absent, returns `(ins, ins)` — the sorted insertion point —
    so a splice keeps the array ordered the same way a full reload would.
    """
    lo = hi = None
    for i, k in enumerate(keys):
        if k == rel_path:
            if lo is None:
                lo = i
            hi = i + 1
        elif hi is not None:
            break  # block ended — rows for one file are contiguous
    if lo is not None:
        return lo, hi
    ins = len(keys)
    for i, k in enumerate(keys):
        if k > rel_path:
            ins = i
            break
    return ins, ins


# --------------------------------------------------------------- generation meta
#
# The matrix caches key on an in-band WRITE GENERATION, not the sidecar file's
# mtime. The sidecars are WAL sqlite: a commit does NOT move the main file's
# mtime — a CHECKPOINT does, and under concurrent connections the checkpoint
# fires whenever the last connection (often a pure reader) closes, at a moment no
# writer runs. So mtime-keyed invalidation BOTH spuriously misses (a checkpoint
# with no content change) AND goes stale (an uncheckpointed commit leaves the
# mtime unmoved). A `meta(key, value)` row bumped inside each write's own
# transaction changes iff the content did. Third occurrence of this class in the
# repo; precedent + rationale: lexstore.cache_token.
#
# One-way legacy fallback: once a sidecar's generation reaches >= 1, the cache
# trusts (epoch, generation, instance) EXCLUSIVELY and stops checking mtime. A
# write from a PRE-generation binary (one that predates this whole mechanism)
# past that point is invisible to invalidation — old and new binaries writing
# the SAME sidecar concurrently is unsupported. Fine for this single-user,
# single-machine-per-sidecar deployment; would need re-litigating for multi-writer.

_INSTANCE_MIN = 1
_INSTANCE_MAX = 2**31 - 1


def _ensure_meta_table(
    conn: sqlite3.Connection, data_table: str, sidecar_name: str
) -> None:
    """Create the `meta(key, value)` generation table if absent (idempotent).

    On first creation (never on a later `_connect()` against the same physical
    file) also writes a random nonzero `instance` row — the ABA guard (F3): a
    sidecar file that gets DELETED and recreated from scratch restarts its
    (epoch, generation) counters at (0, 1), so a still-warm cache from the OLD
    file could otherwise coincidentally match the NEW file's early tokens and
    serve the old file's stale rows forever (impossible with mtime, which a
    brand-new file can't collide with). `instance` is committed immediately
    (this runs before the caller's own write txn opens) so a concurrent reader
    can never observe the meta table without it.

    Logs ONCE when the table is first created over an EXISTING non-empty sidecar
    (a legacy DB written before this binary): its generation reads 0 until the
    first gen-bumping write, and the caches fall back to mtime keying meanwhile.
    """
    existed = (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
        ).fetchone()
        is not None
    )
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value INTEGER)")
    if not existed:
        instance = random.SystemRandom().randint(_INSTANCE_MIN, _INSTANCE_MAX)
        conn.execute(
            "INSERT OR IGNORE INTO meta (key, value) VALUES ('instance', ?)",
            (instance,),
        )
        conn.commit()
        # data_table is a trusted literal ("chunks"/"images"), never user input.
        has_rows = (
            conn.execute(f"SELECT 1 FROM {data_table} LIMIT 1").fetchone() is not None
        )
        if has_rows:
            log.info(
                "%s: created generation-meta over an existing non-empty sidecar "
                "(legacy migration; mtime-keyed cache until the first gen-bumping write)",
                sidecar_name,
            )


def _read_meta_token(conn: sqlite3.Connection) -> tuple[int, int, int]:
    """Return `(epoch, generation, instance)` from the sidecar's meta table.

    A missing key reads as 0 — epoch/generation: a legacy sidecar, or a fresh one
    before its first gen-bumping write; instance: a sidecar migrated before the
    instance nonce existed (self-consistent — see `_ensure_meta_table`: such a
    sidecar always reports 0 here, so a cache built from the SAME physical file
    compares 0 == 0, and only a genuinely NEW file — random nonzero — differs).
    One SELECT is one implicit snapshot, so the triple is internally consistent
    even in autocommit.
    """
    rows = conn.execute(
        "SELECT key, value FROM meta WHERE key IN ('epoch', 'generation', 'instance')"
    ).fetchall()
    d = {k: v for k, v in rows}
    return (
        int(d.get("epoch") or 0),
        int(d.get("generation") or 0),
        int(d.get("instance") or 0),
    )


def _bump_meta(conn: sqlite3.Connection, key: str) -> int:
    """Increment `meta[key]` inside the caller's OPEN write txn; return the new value.

    UPDATE-then-INSERT (never `INSERT OR REPLACE` with a subselect, which yields
    NULL on the missing row). The read-back is stable because the caller holds the
    write lock for the whole transaction.
    """
    cur = conn.execute("UPDATE meta SET value = value + 1 WHERE key = ?", (key,))
    if cur.rowcount == 0:
        conn.execute("INSERT INTO meta (key, value) VALUES (?, 1)", (key,))
    return int(
        conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()[0]
    )


def _reload_reason(old_cache, new_epoch: int, new_gen: int) -> str:
    """Observability tag for a full matrix load: cold|legacy|epoch|genuine.

    `old_cache` is the previous `_EmbCache`/`_ClipCache` (or None) — `.epoch` is
    its captured epoch. This log line is the blind spot that made the WAL-mtime
    bug cost two debugging sessions — every full reload now says WHY, greppable
    in logs/exomem.log.
    """
    if old_cache is None:
        return "cold"
    if new_gen == 0:
        return "legacy"  # gen-0 sidecar: reload was mtime-triggered (version-skew fallback)
    if new_epoch != old_cache.epoch:
        return "epoch"  # a rebuild_all re-embed bumped the epoch
    return "genuine"  # generation (or instance) advanced — an external write


def _peek_sidecar_token(path: Path) -> tuple[int, int, int] | None:
    """Bare-connection freshness probe: `(epoch, generation, instance)`.

    Plain `sqlite3.connect` — NO WAL/synchronous pragmas, NO schema CREATE TABLE
    DDL (mirrors the original `_sidecar_cache_token` pattern; F4). A find touches
    this 4-6x per query (the hot-cache freshness key plus `all_vectors()`, and
    hybrid mode reads both sidecars), so skipping pragma/DDL setup on every peek
    materially cuts the per-call overhead versus routing through `_connect()`.

    Returns `(0, 0, 0)` when the sidecar doesn't exist yet or predates the `meta`
    table — a real, expected state (fresh or legacy sidecar), not a failure.
    Returns `None` when the read itself failed (locked/corrupt sidecar): callers
    holding a warm cache should serve it rather than treat this as invalidation
    (F2) — a plain `stat()` never raised this way, so a bare read here must not
    newly break a caller that previously only paid a filesystem stat.
    """
    if not path.exists():
        return (0, 0, 0)
    try:
        conn = sqlite3.connect(path)
        try:
            has_meta = (
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
                ).fetchone()
                is not None
            )
            return _read_meta_token(conn) if has_meta else (0, 0, 0)
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def _sidecar_cache_token(path: Path) -> tuple[int, int, int]:
    """`(epoch, generation, instance)` for `find._freshness_key` — `(0, 0, 0)` on
    ANY failure (absent, legacy, or a transient read error). A wrong reading here
    only makes find's hot cache MISS (recompute), never serve a stale HIT, so
    failing open to `(0, 0, 0)` is safe (contrast `_peek_sidecar_token`, whose
    `all_vectors()` caller must distinguish a real `(0,0,0)` from a failed read to
    avoid dropping a perfectly good warm cache — see that docstring). Read-only:
    never creates the sidecar. Shared by `EmbeddingIndex.cache_token` /
    `ClipIndex.cache_token`.
    """
    return _peek_sidecar_token(path) or (0, 0, 0)


def _cache_is_fresh(c, path: Path, epoch: int, gen: int, instance: int) -> bool:
    """Does cached token `(c.epoch, c.generation, c.instance, c.mtime)` still
    serve for on-disk `(epoch, gen, instance)`?

    `gen >= 1` → trust the full `(epoch, gen, instance)` triple only — matching
    `instance` too guards the ABA case (F3): a sidecar deleted and recreated from
    scratch restarts (epoch, generation) at (0, 1), which could otherwise
    coincidentally match a still-warm cache from the OLD file.
    `gen == 0` (legacy sidecar, pre-meta) → version-skew fallback to today's
    mtime keying until the first gen-bumping write.

    Shared by `EmbeddingIndex`/`ClipIndex` (F4): both cache NamedTuples expose the
    same `.epoch`/`.generation`/`.instance`/`.mtime` fields regardless of their
    different payloads (metadata+matrix vs paths+frame_ts+matrix).
    """
    if gen >= 1:
        return c.generation == gen and c.epoch == epoch and c.instance == instance
    try:
        return c.mtime == path.stat().st_mtime
    except OSError:
        return False


def _try_serve_cached(c, path: Path):
    """Return `c` unchanged if it can serve the CURRENT on-disk token, else None.

    Centralizes the double-checked-locking freshness gate for both classes'
    `all_vectors()` (F4). `_peek_sidecar_token` returning `None` means the read
    itself failed (locked/corrupt sidecar, F2): a WARM cache is served anyway
    (stale-by-seconds under contention is acceptable and matches busy semantics)
    — only a COLD cache (`c is None`) falls through (returns None) to the
    caller's full-load attempt, which has its own connect/BEGIN and can still
    raise on a genuinely broken sidecar (that failure mode is unchanged from
    before this fix — this function only protects the fast, no-lock-needed path).
    """
    token = _peek_sidecar_token(path)
    if token is None:
        if c is not None:
            log.warning(
                "sidecar token read failed for %s; serving the warm cache", path
            )
        return c
    if c is not None and _cache_is_fresh(c, path, *token):
        return c
    return None


class _EmbCache(NamedTuple):
    """EmbeddingIndex's in-memory matrix cache. `(epoch, generation, instance)` is
    the write token (F1-F3); `mtime` is retained only for the gen==0 legacy
    fallback. `metadata[i] = (file_path, chunk_idx)`; `matrix[i]` = its vector."""

    epoch: int
    generation: int
    instance: int
    mtime: float
    metadata: list[tuple[str, int]]
    matrix: np.ndarray


class _ClipCache(NamedTuple):
    """ClipIndex's in-memory matrix cache — mirrors `_EmbCache`. `frame_ts[i]` is
    None for an image row, seconds for a video keyframe row; `paths`/`frame_ts`/
    `matrix` are parallel arrays."""

    epoch: int
    generation: int
    instance: int
    mtime: float
    paths: list[str]
    frame_ts: list[float | None]
    matrix: np.ndarray


def _vec_gate(index, conn: sqlite3.Connection) -> bool:
    """Shared vec0 policy ladder for EmbeddingIndex/ClipIndex on one connection.

    Duck-typed over the index's vec state (`_vec`, `_vec_failed`, `_vec_ready`,
    `_vec_quant_synced`): backend gate → extension loadable on this connection →
    tables created + blob↔vec counts synced (memoized per instance; re-run once
    more when binary quant turns on later, to synthesize the bin table). Any sync
    failure retires vec for this instance — the numpy scan serves from then on.

    numpy is the default backend, so this gate returns False (and every dual-write
    site below is skipped) unless `EXOMEM_VEC_BACKEND=sqlite-vec` opts in. While
    vec0 is off, the shadow tables drift as the blob tables advance without them;
    that drift is self-healing — the first opt-in call reaches `ensure_synced`,
    whose count-mismatch check rebuilds the vec rows from the blobs in pure SQL.
    """
    if index._vec_failed or vecstore.backend() == "numpy":
        return False
    if not index._vec.try_load(conn):
        return False
    quant = vecstore.quant_mode() == "binary"
    if index._vec_ready is None or (quant and not index._vec_quant_synced):
        try:
            index._vec.ensure_synced(conn, quant=quant)
            index._vec_ready = True
            if quant:
                index._vec_quant_synced = True
        except sqlite3.Error as e:
            log.warning(
                "vec sync failed for %s (%s); in-memory scan serves this process",
                index.path, e,
            )
            index._vec_failed = True
            return False
    return True


class EmbeddingIndex:
    """Per-vault sqlite sidecar holding chunk-level vectors.

    The matrix returned by `all_vectors()` is cached per-process and
    invalidated by an in-band WRITE GENERATION (a `meta` row bumped inside every
    write's own transaction), NOT the sidecar mtime — see the generation-meta
    note above for why WAL-checkpoint timing makes mtime keying both spuriously
    miss and go stale. When the vec0 backend is active (`vecstore`), `search()` is served by a
    SQL-native KNN over shadow tables in the same sidecar instead, and this
    matrix stays cold — `all_vectors()` remains for audit's all-pairs sweep
    and the numpy fallback.

    numpy-lite (2026-07-04): the cache holds ONLY `(file_path, chunk_idx)`
    metadata + the float32 matrix — chunk TEXT is never resident. Text was
    most of the numpy backend's memory bill at scale (~2GB of a ~3.5GB RSS at
    200k chunks); the top-k winners' texts are point-lookups on the
    `(file_path, chunk_idx)` PRIMARY KEY at search time, exactly how the vec0
    path already hydrates metadata by rowid.
    """

    def __init__(self, vault_root: Path):
        self.vault_root = vault_root
        self.path = sidecar_path(vault_root)
        self._cache: _EmbCache | None = None
        # Guards in-memory cache mutation only (never held across a sqlite write).
        # Reentrant so rebuild_all()-style nesting can't self-deadlock.
        self._lock = threading.RLock()
        # vec0 backend state (see _vec_gate): sync memo + per-instance retirement.
        self._vec = vecstore.SqliteVecStore("chunks", "vector", VECTOR_DIM, "vec_chunks")
        self._vec_ready: bool | None = None
        self._vec_quant_synced = False
        self._vec_failed = False

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        _apply_sidecar_pragmas(conn)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chunks (
                file_path TEXT NOT NULL,
                chunk_idx INTEGER NOT NULL,
                chunk_text TEXT NOT NULL,
                vector BLOB NOT NULL,
                file_mtime REAL NOT NULL,
                PRIMARY KEY (file_path, chunk_idx)
            )
            """
        )
        _ensure_meta_table(conn, "chunks", self.path.name)
        return conn

    def upsert_file(
        self,
        rel_path: str,
        chunks: list[str],
        vectors: np.ndarray,
        mtime: float,
    ) -> None:
        """Replace all rows for `rel_path` in a single transaction."""
        if len(chunks) != len(vectors):
            raise ValueError(
                f"chunks/vectors length mismatch for {rel_path}: "
                f"{len(chunks)} vs {len(vectors)}"
            )
        conn = self._connect()
        try:
            vec_on = _vec_gate(self, conn)
            with conn:
                if vec_on:
                    # BEFORE the blob delete — the subquery needs the old rowids.
                    self._vec.dual_delete(conn, "file_path = ?", (rel_path,))
                conn.execute("DELETE FROM chunks WHERE file_path = ?", (rel_path,))
                if chunks:
                    rows = [
                        (rel_path, i, chunks[i], vectors[i].astype(np.float32).tobytes(), mtime)
                        for i in range(len(chunks))
                    ]
                    conn.executemany(
                        "INSERT INTO chunks "
                        "(file_path, chunk_idx, chunk_text, vector, file_mtime) "
                        "VALUES (?, ?, ?, ?, ?)",
                        rows,
                    )
                    if vec_on:
                        self._vec.dual_insert(conn, "file_path = ?", (rel_path,))
                # Bump the write generation INSIDE this txn, then read back the
                # FULL (epoch, generation, instance) token — stable under the
                # write lock. The cache keys on it, not the mtime.
                _bump_meta(conn, "generation")
                own_epoch, own_gen, own_instance = _read_meta_token(conn)
        finally:
            conn.close()
        # Patch the shared in-memory matrix in place instead of nulling it, so a
        # concurrent find() doesn't pay a full O(vault) reload for this one write.
        # numpy-lite: metadata rows carry no chunk text (see class docstring).
        new_meta = [(rel_path, i) for i in range(len(chunks))]
        new_vecs = np.asarray(vectors, dtype=np.float32) if chunks else None
        self._patch_cache(rel_path, new_meta, new_vecs, own_epoch, own_gen, own_instance)

    def delete_file(self, rel_path: str) -> None:
        conn = self._connect()
        try:
            vec_on = _vec_gate(self, conn)
            with conn:
                if vec_on:
                    self._vec.dual_delete(conn, "file_path = ?", (rel_path,))
                conn.execute("DELETE FROM chunks WHERE file_path = ?", (rel_path,))
                _bump_meta(conn, "generation")
                own_epoch, own_gen, own_instance = _read_meta_token(conn)
        finally:
            conn.close()
        self._patch_cache(rel_path, [], None, own_epoch, own_gen, own_instance)

    def _patch_cache(
        self,
        rel_path: str,
        new_meta: list[tuple[str, int]],
        new_vecs: np.ndarray | None,
        own_epoch: int,
        own_gen: int,
        own_instance: int,
    ) -> None:
        """Splice one file's rows into the cached matrix (copy-on-write) — ONLY
        when this write is contiguous with the CURRENT cache: `own_epoch ==
        cached.epoch AND own_instance == cached.instance AND own_gen ==
        cached.generation + 1`. On ANY mismatch, the splice is skipped ENTIRELY —
        content is NOT spliced and the label does NOT advance — leaving the cache
        exactly as it was; the resulting token mismatch heals via a full reload on
        the next `all_vectors()` (cheap enough — Phase 1 semantics).

        This gates content and label TOGETHER because splicing content whose
        label can't (yet) advance is unsafe on its own (a corrected design point:
        an earlier version of this cache spliced content unconditionally and only
        gated the label, which does not prevent corruption). Proven trace: writer
        A upserts file F (capturing generation 5) then stalls before calling this;
        writer B upserts the SAME file F (generation 6) and patches immediately —
        contiguous, so B's rows land and the label advances to 6; A then resumes
        and calls this with its OWN (now stale) generation 5 and its OLDER rows —
        if content were spliced unconditionally (as before), A's stale rows would
        overwrite B's current ones while the label still reads a plausible value,
        risking B's genuinely-current rows being replaced by A's stale ones. Never
        use `max()` on the generation either, for the same reason: it would let
        the cache claim a generation whose rows it never received.

        Builds fresh `metadata`/`matrix` and atomically swaps `self._cache`; never
        mutates the arrays a concurrent reader may be holding. Best-effort: any
        inconsistency (post-gate) drops the cache to None so the next
        `all_vectors()` does a safe full reload. Leaves a cold (`None`) cache
        cold — the next read loads.
        """
        with self._lock:
            c = self._cache
            if c is None:
                return
            if own_epoch != c.epoch or own_instance != c.instance or own_gen != c.generation + 1:
                return  # not contiguous with what THIS cache holds -> never splice
            try:
                lo, hi = _file_block([m[0] for m in c.metadata], rel_path)
                new_metadata = c.metadata[:lo] + list(new_meta) + c.metadata[hi:]
                parts = [c.matrix[:lo]]
                if new_vecs is not None and new_vecs.shape[0]:
                    parts.append(new_vecs)
                parts.append(c.matrix[hi:])
                parts = [p for p in parts if p.shape[0]]
                new_matrix = (
                    np.concatenate(parts, axis=0)
                    if parts
                    else np.zeros((0, VECTOR_DIM), dtype=np.float32)
                )
                if len(new_metadata) != new_matrix.shape[0]:
                    raise ValueError(
                        f"splice invariant broken for {rel_path}: "
                        f"{len(new_metadata)} meta rows vs {new_matrix.shape[0]} vectors"
                    )
                self._cache = _EmbCache(
                    c.epoch, own_gen, c.instance, c.mtime, new_metadata, new_matrix
                )
            except Exception as e:  # noqa: BLE001 — self-heal, never break a write
                log.warning("embedding matrix splice failed (%s); dropping cache", e)
                self._cache = None

    def all_vectors(self) -> tuple[list[tuple[str, int]], np.ndarray]:
        """Return `(metadata, matrix)` cached until the sidecar's write generation
        (or epoch) advances — NOT its mtime (see the class + generation-meta notes).

        metadata[i] = (file_path, chunk_idx); matrix[i] = vector. Chunk text
        is deliberately NOT here (numpy-lite — see class docstring); fetch the
        winners' texts via `_texts_for` when needed.
        """
        if not self.path.exists():
            return [], np.zeros((0, VECTOR_DIM), dtype=np.float32)
        # Snapshot the cache tuple ONCE: another thread may swap or null it between
        # reads. This fast path takes no lock — the common case.
        c = self._cache
        served = _try_serve_cached(c, self.path)
        if served is not None:
            return served.metadata, served.matrix
        with self._lock:
            # Re-check under the lock: another thread may have loaded while we
            # waited, or the fast-path token read may have failed transiently.
            c = self._cache
            served = _try_serve_cached(c, self.path)
            if served is not None:
                return served.metadata, served.matrix
            loaded = self._load_all_rows()
            log.info(
                "embedding matrix full load: reason=%s rows=%d gen=%d epoch=%d",
                _reload_reason(c, loaded.epoch, loaded.generation),
                len(loaded.metadata), loaded.generation, loaded.epoch,
            )
            self._cache = loaded
            return loaded.metadata, loaded.matrix

    def _load_all_rows(self) -> _EmbCache:
        """Full reload from the sidecar → an `_EmbCache`.

        Reads the meta token AND the rows inside ONE explicit `BEGIN` so they
        are a single consistent snapshot — python sqlite3 in autocommit runs each
        bare SELECT in its OWN snapshot, so a naive two-statement read could pair a
        generation with rows from a different write. This is the O(vault) `SELECT`
        + `np.stack` the incremental cache exists to avoid paying per find; kept a
        named method so tests can count genuine full reloads. numpy-lite: chunk
        text is neither SELECTed nor retained; file_path strings are interned so N
        rows of one file share a single str object.
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            try:
                epoch, gen, instance = _read_meta_token(conn)
                rows = conn.execute(
                    "SELECT file_path, chunk_idx, vector FROM chunks "
                    "ORDER BY file_path, chunk_idx"
                ).fetchall()
            finally:
                conn.rollback()  # read-only txn — release the snapshot
        finally:
            conn.close()
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            mtime = 0.0
        if not rows:
            return _EmbCache(
                epoch, gen, instance, mtime, [], np.zeros((0, VECTOR_DIM), dtype=np.float32)
            )
        metadata: list[tuple[str, int]] = []
        vectors: list[np.ndarray] = []
        for fp, idx, blob in rows:
            metadata.append((sys.intern(fp), idx))
            vectors.append(np.frombuffer(blob, dtype=np.float32))
        return _EmbCache(epoch, gen, instance, mtime, metadata, np.stack(vectors, axis=0))

    def search(
        self, query_vec: np.ndarray, k: int
    ) -> list[tuple[str, int, str, float]]:
        """Top-k chunk hits: list of `(file_path, chunk_idx, chunk_text, score)`.

        Backend ladder: vec0 KNN in the sidecar when available (full-precision by
        default — exact, rank-identical to the scan below; binary+rescore when
        `EXOMEM_VEC_QUANT=binary`), otherwise the in-memory numpy scan. Every vec
        failure mode falls through to the scan — search never breaks on vec0.
        """
        vec_hits = self._vec_search(query_vec, k)
        if vec_hits is not None:
            return vec_hits
        metadata, matrix = self.all_vectors()
        if not metadata:
            return []
        # query_vec is (768,) normalized; matrix is (N, 768) normalized.
        scores = matrix @ query_vec.astype(np.float32, copy=False)
        k_eff = min(k, len(scores))
        if k_eff <= 0:
            return []
        # argpartition is O(N), then sort the top-k slice.
        top_idx = np.argpartition(-scores, k_eff - 1)[:k_eff]
        top_idx = top_idx[np.argsort(-scores[top_idx])]
        top = [(metadata[i][0], metadata[i][1], float(scores[i])) for i in top_idx]
        # numpy-lite: hydrate only the winners' texts (PK point-lookups).
        try:
            texts = self._texts_for([(fp, ci) for fp, ci, _ in top])
        except Exception as e:  # noqa: BLE001 — text hydration must never break search
            log.warning("chunk-text fetch failed (%s); returning hits without text", e)
            texts = {}
        return [(fp, ci, texts.get((fp, ci), ""), score) for fp, ci, score in top]

    def _texts_for(self, pairs: list[tuple[str, int]]) -> dict[tuple[str, int], str]:
        """chunk_text for `(file_path, chunk_idx)` pairs — search's top-k only.

        The in-memory cache holds no chunk text (numpy-lite), so the numpy
        rung hydrates its winners here: point-lookups on the table's
        `(file_path, chunk_idx)` PRIMARY KEY, batched to stay far under
        SQLite's bound-variable cap.
        """
        out: dict[tuple[str, int], str] = {}
        if not pairs:
            return out
        conn = self._connect()
        try:
            batch_size = 150  # 2 bound params per pair
            for s in range(0, len(pairs), batch_size):
                batch = pairs[s:s + batch_size]
                where = " OR ".join(
                    "(file_path = ? AND chunk_idx = ?)" for _ in batch
                )
                params: list = []
                for fp, ci in batch:
                    params.extend((fp, ci))
                rows = conn.execute(
                    f"SELECT file_path, chunk_idx, chunk_text FROM chunks WHERE {where}",
                    params,
                ).fetchall()
                for fp, ci, txt in rows:
                    out[(fp, ci)] = txt
        finally:
            conn.close()
        return out

    def _vec_search(
        self, query_vec: np.ndarray, k: int
    ) -> list[tuple[str, int, str, float]] | None:
        """vec0 KNN, or None when the backend can't serve (the scan takes over).

        Never creates the sidecar file on a read path (a missing sidecar keeps the
        historical `[]`-via-scan semantics), and never raises: a runtime vec
        failure logs, retires vec for this instance, and returns None.
        """
        if self._vec_failed or vecstore.backend() == "numpy" or vecstore.load_failed():
            return None
        if not self.path.exists():
            return None
        try:
            conn = self._connect()
            try:
                if not _vec_gate(self, conn):
                    return None
                quant = vecstore.quant_mode() == "binary"
                pairs = self._vec.knn(conn, query_vec, k, quant=quant)
                if not pairs:
                    return []
                ids = [rid for rid, _ in pairs]
                placeholders = ",".join("?" * len(ids))
                rows = conn.execute(
                    "SELECT rowid, file_path, chunk_idx, chunk_text FROM chunks "
                    f"WHERE rowid IN ({placeholders})",
                    ids,
                ).fetchall()
                by_id = {r[0]: r for r in rows}
                return [
                    (by_id[rid][1], by_id[rid][2], by_id[rid][3], score)
                    for rid, score in pairs
                    if rid in by_id
                ]
            finally:
                conn.close()
        except Exception as e:  # noqa: BLE001 — vec failure must never break search
            log.warning(
                "vec search failed for %s (%s); falling back to the in-memory scan",
                self.path, e,
            )
            self._vec_failed = True
            return None

    def file_mtimes(self) -> dict[str, float]:
        """Map each indexed `file_path` → its max stored `file_mtime` (one query).

        The idempotency oracle for `index_incremental`: a file whose on-disk mtime
        does not exceed this value is already current in the sidecar and is skipped.
        Empty dict when the sidecar has not been created yet.
        """
        if not self.path.exists():
            return {}
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT file_path, MAX(file_mtime) FROM chunks GROUP BY file_path"
            ).fetchall()
        finally:
            conn.close()
        return {r[0]: r[1] for r in rows if isinstance(r[0], str) and r[1] is not None}

    def rebuild_all(self) -> int:
        """Wipe + re-embed every compiled .md the index scope covers. Returns row count.

        Scope is `index_scope()` (`EXOMEM_INDEX_SCOPE`): `"kb"` (default) walks
        `Knowledge Base/` only — byte-identical to the historical behavior;
        `"vault"` walks the whole vault (`vault.walk_vault_md`) so notes outside
        `Knowledge Base/` become semantically searchable. Both honor
        `access.is_indexable` and the shared `_is_embeddable_path` /
        `_chunks_for_page` filtering, so only the walked file SET differs.
        """
        from . import access
        from . import find as find_module

        scope = index_scope()
        kb = self.vault_root / kb_dirname()
        # KB scope with no Knowledge Base/ is a no-op that must NOT wipe (historical
        # early return). Vault scope always proceeds — it indexes the wider tree.
        if scope == "kb" and not kb.is_dir():
            return 0
        # Wipe whole table — easier than per-file diff for a one-shot rebuild.
        conn = self._connect()
        try:
            vec_on = _vec_gate(self, conn)
            with conn:
                if vec_on:
                    self._vec.wipe(conn)
                conn.execute("DELETE FROM chunks")
        finally:
            conn.close()
        self._cache = None

        all_chunks: list[tuple[str, list[str], float]] = []
        for md in _index_walk(self.vault_root):
            if not _is_embeddable_path(md):
                continue
            page = find_module._CACHE.get(md, self.vault_root)
            if page is None:
                continue
            if not access.is_indexable(self.vault_root, page.rel_path):
                continue  # excluded tree (_access.yaml) — keep it out of the index
            chunks = _chunks_for_page(self.vault_root, page)
            if not chunks:
                continue
            all_chunks.append((page.rel_path, chunks, page.mtime))

        if not all_chunks:
            return 0

        # Batch-embed across all files at once for GPU efficiency.
        flat_texts: list[str] = []
        for _, chunks, _ in all_chunks:
            flat_texts.extend(chunks)
        log.info("rebuild_embeddings: embedding %d chunks from %d files",
                 len(flat_texts), len(all_chunks))
        vectors = embed_texts(flat_texts, is_query=False)

        # Bulk write in ONE transaction. Per-file upsert_file() calls would each
        # open a connection, fsync, and splice the in-memory matrix — O(N²) copies
        # plus N fsyncs. Build every row, wipe + executemany once, then leave the
        # cache null (set at the top) so the next all_vectors() does ONE full load.
        insert_rows: list[tuple[str, int, str, bytes, float]] = []
        offset = 0
        total = 0
        for rel_path, chunks, mtime in all_chunks:
            for i, ch in enumerate(chunks):
                insert_rows.append(
                    (rel_path, i, ch, vectors[offset + i].astype(np.float32).tobytes(), mtime)
                )
            offset += len(chunks)
            total += len(chunks)
        conn = self._connect()
        try:
            vec_on = _vec_gate(self, conn)
            with conn:
                conn.execute("DELETE FROM chunks")
                conn.executemany(
                    "INSERT INTO chunks "
                    "(file_path, chunk_idx, chunk_text, vector, file_mtime) "
                    "VALUES (?, ?, ?, ?, ?)",
                    insert_rows,
                )
                if vec_on:
                    # One whole-table INSERT..SELECT from the fresh blobs — the
                    # bulk analog of the per-file dual-write.
                    self._vec.wipe(conn)
                    self._vec.repopulate_all(conn)
                # Bump generation (monotonic write counter) AND epoch (re-embed
                # marker) in the FINAL txn only — never the wipe txn above. A WARM
                # reader whose cache still matches the PRE-bump token keeps serving
                # its correct pre-rebuild snapshot through the wipe→final-txn gap
                # (the whole point of gating patch-cache on contiguity, F1). A COLD
                # reader (or any cache miss) racing that same gap instead loads the
                # wipe's EMPTY table under that pre-bump token, and would keep
                # serving empty until this commit moves the token — the same
                # exposure a full reload always had racing a wipe/rebuild window,
                # unchanged by this PR. epoch catches re-embeds that changed no
                # file mtimes.
                _bump_meta(conn, "generation")
                _bump_meta(conn, "epoch")
        finally:
            conn.close()
        with self._lock:
            self._cache = None
        return total

    @staticmethod
    def cache_token(vault_root: Path) -> tuple[int, int, int]:
        """`(epoch, generation, instance)` for this vault's embedding sidecar —
        the freshness signal find keys its hot cache on. `(0, 0, 0)` when the
        sidecar is absent or pre-meta (legacy); find's walk triples cover
        invalidation meanwhile.

        Deliberately NOT the sidecar file's mtime: WAL-checkpoint timing moves the
        mtime independent of content (spurious misses) and an uncheckpointed commit
        leaves it unmoved (stale hits). The in-band generation is bumped inside
        every write's transaction, so it changes iff the content did; `instance`
        additionally guards the ABA case where the sidecar was deleted and
        recreated from scratch (see `_ensure_meta_table`). Precedent and
        rationale: lexstore.cache_token. Read-only: never creates the sidecar.
        """
        path = sidecar_path(vault_root)
        return _sidecar_cache_token(path)


class ClipIndex:
    """Per-vault sqlite sidecar of CLIP image vectors — one vector per image.

    Mirrors EmbeddingIndex (generation-cached matrix, cosine search) but keyed by
    image file with no chunking. Lives in its own `.clip.sqlite` so the bge text index is
    untouched and the two evolve independently.
    """

    def __init__(self, vault_root: Path):
        self.vault_root = vault_root
        self.path = clip_sidecar_path(vault_root)
        self._cache: _ClipCache | None = None
        self._lock = threading.RLock()
        # vec0 backend state (see _vec_gate) — mirrors EmbeddingIndex.
        self._vec = vecstore.SqliteVecStore("images", "vector", CLIP_DIM, "vec_images")
        self._vec_ready: bool | None = None
        self._vec_quant_synced = False
        self._vec_failed = False

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        _apply_sidecar_pragmas(conn)
        # Multi-vector schema: one row per image (frame_ts NULL) OR one row per
        # video keyframe (frame_ts = seconds). Composite PK keys frames within a
        # file. NOTE: SQLite treats NULL as DISTINCT in a PRIMARY KEY/UNIQUE index,
        # so two image rows (same file_path, NULL frame_ts) do NOT collide — every
        # write path below uses delete-then-insert rather than INSERT OR REPLACE.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS images (
                file_path  TEXT NOT NULL,
                frame_ts   REAL,
                vector     BLOB NOT NULL,
                file_mtime REAL NOT NULL,
                PRIMARY KEY (file_path, frame_ts)
            )
            """
        )
        self._migrate_add_frame_ts(conn)
        _ensure_meta_table(conn, "images", self.path.name)
        return conn

    @staticmethod
    def _migrate_add_frame_ts(conn: sqlite3.Connection) -> None:
        """Upgrade a pre-existing single-vector `images` table in place.

        Old schema: `images(file_path PK, vector, file_mtime)` — no `frame_ts`.
        SQLite can't ALTER a PRIMARY KEY, so rebuild the table preserving rows
        (existing image vectors are worth keeping): old rows become image rows
        (`frame_ts` NULL). Idempotent — no-op once `frame_ts` exists. The CREATE
        above already made the new table when the sidecar is fresh; this only
        fires when an OLD table is present.
        """
        cols = [r[1] for r in conn.execute("PRAGMA table_info(images)").fetchall()]
        if "frame_ts" in cols:
            return
        with conn:
            conn.execute(
                """
                CREATE TABLE images_new (
                    file_path  TEXT NOT NULL,
                    frame_ts   REAL,
                    vector     BLOB NOT NULL,
                    file_mtime REAL NOT NULL,
                    PRIMARY KEY (file_path, frame_ts)
                )
                """
            )
            conn.execute(
                "INSERT INTO images_new (file_path, frame_ts, vector, file_mtime) "
                "SELECT file_path, NULL, vector, file_mtime FROM images"
            )
            conn.execute("DROP TABLE images")
            conn.execute("ALTER TABLE images_new RENAME TO images")
        log.info("ClipIndex: migrated images table to multi-vector schema (frame_ts)")

    def upsert(self, rel_path: str, vector: np.ndarray, mtime: float) -> None:
        """Store one image vector (frame_ts NULL). Delete-then-insert: NULL is
        DISTINCT in the PK, so INSERT OR REPLACE would duplicate the row."""
        conn = self._connect()
        try:
            vec_on = _vec_gate(self, conn)
            with conn:
                if vec_on:
                    # The vec delete carries the SAME partial predicate: replace
                    # only the image (NULL-ts) row, keep the keyframe rows.
                    self._vec.dual_delete(
                        conn, "file_path = ? AND frame_ts IS NULL", (rel_path,)
                    )
                conn.execute(
                    "DELETE FROM images WHERE file_path = ? AND frame_ts IS NULL",
                    (rel_path,),
                )
                conn.execute(
                    "INSERT INTO images (file_path, frame_ts, vector, file_mtime) "
                    "VALUES (?, NULL, ?, ?)",
                    (rel_path, vector.astype(np.float32).tobytes(), mtime),
                )
                if vec_on:
                    self._vec.dual_insert(
                        conn, "file_path = ? AND frame_ts IS NULL", (rel_path,)
                    )
                _bump_meta(conn, "generation")
                own_epoch, own_gen, own_instance = _read_meta_token(conn)
        finally:
            conn.close()
        # Mirror the partial DELETE: replace only the image (NULL-ts) row, keep any
        # existing video keyframe rows for this file.
        self._patch_cache(
            rel_path,
            [rel_path],
            [None],
            np.asarray(vector, dtype=np.float32).reshape(1, -1),
            own_epoch,
            own_gen,
            own_instance,
            images_only=True,
        )

    def upsert_frames(
        self, rel_path: str, frames: list[tuple[float, np.ndarray]], mtime: float
    ) -> None:
        """Replace all vectors for a video with N per-keyframe rows in one txn.

        Clears any prior rows for `rel_path` first — including a stale mean-pooled
        NULL-ts row from the old single-vector path — then inserts one row per
        `(timestamp_seconds, vector)`. No-op on an empty frame list (caller soft-skips).
        """
        if not frames:
            return
        conn = self._connect()
        try:
            vec_on = _vec_gate(self, conn)
            with conn:
                if vec_on:
                    self._vec.dual_delete(conn, "file_path = ?", (rel_path,))
                conn.execute("DELETE FROM images WHERE file_path = ?", (rel_path,))
                conn.executemany(
                    "INSERT INTO images (file_path, frame_ts, vector, file_mtime) "
                    "VALUES (?, ?, ?, ?)",
                    [
                        (rel_path, float(ts), vec.astype(np.float32).tobytes(), mtime)
                        for ts, vec in frames
                    ],
                )
                if vec_on:
                    self._vec.dual_insert(conn, "file_path = ?", (rel_path,))
                _bump_meta(conn, "generation")
                own_epoch, own_gen, own_instance = _read_meta_token(conn)
        finally:
            conn.close()
        # Whole-file DELETE + re-insert → block-replace with the frames, ordered by
        # timestamp to match the load's `ORDER BY file_path, frame_ts`.
        ordered = sorted(frames, key=lambda f: f[0])
        self._patch_cache(
            rel_path,
            [rel_path] * len(ordered),
            [float(ts) for ts, _ in ordered],
            np.stack([np.asarray(vec, dtype=np.float32) for _, vec in ordered], axis=0),
            own_epoch,
            own_gen,
            own_instance,
        )

    def delete(self, rel_path: str) -> None:
        conn = self._connect()
        try:
            vec_on = _vec_gate(self, conn)
            with conn:
                if vec_on:
                    self._vec.dual_delete(conn, "file_path = ?", (rel_path,))
                conn.execute("DELETE FROM images WHERE file_path = ?", (rel_path,))
                _bump_meta(conn, "generation")
                own_epoch, own_gen, own_instance = _read_meta_token(conn)
        finally:
            conn.close()
        self._patch_cache(rel_path, [], [], None, own_epoch, own_gen, own_instance)

    def _patch_cache(
        self,
        rel_path: str,
        new_paths: list[str],
        new_frame_ts: list[float | None],
        new_vecs: np.ndarray | None,
        own_epoch: int,
        own_gen: int,
        own_instance: int,
        *,
        images_only: bool = False,
    ) -> None:
        """Splice one file's CLIP rows into the cached matrix (copy-on-write) —
        ONLY when contiguous with the CURRENT cache: `own_epoch == cached.epoch
        AND own_instance == cached.instance AND own_gen == cached.generation + 1`.
        On ANY mismatch, skip the splice ENTIRELY (no content change, no label
        advance) — see `EmbeddingIndex._patch_cache` for the full corruption
        rationale (a delayed/out-of-order patch must never overwrite newer rows
        with stale ones just because its own label looks superficially valid).

        `images_only=True` keeps existing video keyframe rows (frame_ts NOT NULL)
        and replaces only the image (NULL-ts) row, mirroring `upsert()`'s partial
        DELETE. Otherwise the file's whole block is replaced. Best-effort: any
        inconsistency (post-gate) drops the cache to None for a safe full reload
        next read.
        """
        with self._lock:
            c = self._cache
            if c is None:
                return
            if own_epoch != c.epoch or own_instance != c.instance or own_gen != c.generation + 1:
                return  # not contiguous with what THIS cache holds -> never splice
            try:
                paths, frame_ts, matrix = c.paths, c.frame_ts, c.matrix
                lo, hi = _file_block(paths, rel_path)
                if images_only and hi > lo:
                    keep = [j for j in range(lo, hi) if frame_ts[j] is not None]
                    block_paths = [paths[j] for j in keep] + list(new_paths)
                    block_ts = [frame_ts[j] for j in keep] + list(new_frame_ts)
                    keep_mat = (
                        matrix[keep] if keep else np.zeros((0, CLIP_DIM), dtype=np.float32)
                    )
                    block_parts = [keep_mat]
                    if new_vecs is not None and new_vecs.shape[0]:
                        block_parts.append(new_vecs)
                else:
                    block_paths = list(new_paths)
                    block_ts = list(new_frame_ts)
                    block_parts = (
                        [new_vecs] if (new_vecs is not None and new_vecs.shape[0]) else []
                    )
                out_paths = paths[:lo] + block_paths + paths[hi:]
                out_ts = frame_ts[:lo] + block_ts + frame_ts[hi:]
                parts = [matrix[:lo], *block_parts, matrix[hi:]]
                parts = [p for p in parts if p.shape[0]]
                out_matrix = (
                    np.concatenate(parts, axis=0)
                    if parts
                    else np.zeros((0, CLIP_DIM), dtype=np.float32)
                )
                if not (len(out_paths) == len(out_ts) == out_matrix.shape[0]):
                    raise ValueError(
                        f"CLIP splice invariant broken for {rel_path}: "
                        f"{len(out_paths)} paths / {len(out_ts)} ts / {out_matrix.shape[0]} vecs"
                    )
                self._cache = _ClipCache(
                    c.epoch, own_gen, c.instance, c.mtime, out_paths, out_ts, out_matrix
                )
            except Exception as e:  # noqa: BLE001 — self-heal, never break a write
                log.warning("CLIP matrix splice failed (%s); dropping cache", e)
                self._cache = None

    def has(self, rel_path: str) -> bool:
        """True if this file has ANY vector row (image or video). Correct idempotency
        for IMAGES (one row); for VIDEOS use `has_frames` — a video with only a stale
        single-vector (frame_ts NULL) row from the old path returns True here but still
        needs per-keyframe re-indexing."""
        if not self.path.exists():
            return False
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT 1 FROM images WHERE file_path = ?", (rel_path,)
            ).fetchone()
        finally:
            conn.close()
        return row is not None

    def has_frames(self, rel_path: str) -> bool:
        """True if this file has at least one PER-KEYFRAME vector (frame_ts NOT NULL).

        The correct idempotency check for VIDEO: a video carrying only a legacy
        single-vector row (frame_ts NULL, from the pre-multi-vector path) returns
        False here, so backfill/worker re-index it per-keyframe instead of skipping it.
        """
        if not self.path.exists():
            return False
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT 1 FROM images WHERE file_path = ? AND frame_ts IS NOT NULL",
                (rel_path,),
            ).fetchone()
        finally:
            conn.close()
        return row is not None

    def all_vectors(self) -> tuple[list[str], list[float | None], np.ndarray]:
        """Returns parallel `(file_paths, frame_ts_list, matrix)`, cached until the
        write generation advances (NOT the mtime — see EmbeddingIndex.all_vectors).
        frame_ts is None for image rows, a float (seconds) for video keyframe rows."""
        if not self.path.exists():
            return [], [], np.zeros((0, CLIP_DIM), dtype=np.float32)
        c = self._cache  # snapshot once — a concurrent writer may swap/null it
        served = _try_serve_cached(c, self.path)
        if served is not None:
            return served.paths, served.frame_ts, served.matrix
        with self._lock:
            # Re-check under the lock (see EmbeddingIndex.all_vectors).
            c = self._cache
            served = _try_serve_cached(c, self.path)
            if served is not None:
                return served.paths, served.frame_ts, served.matrix
            loaded = self._load_all_rows()
            log.info(
                "CLIP matrix full load: reason=%s rows=%d gen=%d epoch=%d",
                _reload_reason(c, loaded.epoch, loaded.generation),
                len(loaded.paths), loaded.generation, loaded.epoch,
            )
            self._cache = loaded
            return loaded.paths, loaded.frame_ts, loaded.matrix

    def _load_all_rows(self) -> _ClipCache:
        """Full reload → a `_ClipCache`, reading the meta token and rows in ONE
        `BEGIN` snapshot (see EmbeddingIndex._load_all_rows)."""
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            try:
                epoch, gen, instance = _read_meta_token(conn)
                rows = conn.execute(
                    "SELECT file_path, frame_ts, vector FROM images "
                    "ORDER BY file_path, frame_ts"
                ).fetchall()
            finally:
                conn.rollback()  # read-only txn — release the snapshot
        finally:
            conn.close()
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            mtime = 0.0
        if not rows:
            return _ClipCache(
                epoch, gen, instance, mtime, [], [], np.zeros((0, CLIP_DIM), dtype=np.float32)
            )
        paths = [r[0] for r in rows]
        frame_ts = [r[1] for r in rows]
        matrix = np.stack([np.frombuffer(r[2], dtype=np.float32) for r in rows], axis=0)
        return _ClipCache(epoch, gen, instance, mtime, paths, frame_ts, matrix)

    @staticmethod
    def cache_token(vault_root: Path) -> tuple[int, int, int]:
        """`(epoch, generation, instance)` for this vault's CLIP sidecar —
        `(0, 0, 0)` when absent or pre-meta. Read-only (mirrors
        EmbeddingIndex.cache_token)."""
        return _sidecar_cache_token(clip_sidecar_path(vault_root))

    def _vec_search(
        self, query_vec: np.ndarray, k: int
    ) -> list[tuple[str, float | None, float]] | None:
        """vec0 KNN, or None when the backend can't serve — mirrors
        `EmbeddingIndex._vec_search` (no sidecar creation on read, never raises)."""
        if self._vec_failed or vecstore.backend() == "numpy" or vecstore.load_failed():
            return None
        if not self.path.exists():
            return None
        try:
            conn = self._connect()
            try:
                if not _vec_gate(self, conn):
                    return None
                quant = vecstore.quant_mode() == "binary"
                pairs = self._vec.knn(conn, query_vec, k, quant=quant)
                if not pairs:
                    return []
                ids = [rid for rid, _ in pairs]
                placeholders = ",".join("?" * len(ids))
                rows = conn.execute(
                    "SELECT rowid, file_path, frame_ts FROM images "
                    f"WHERE rowid IN ({placeholders})",
                    ids,
                ).fetchall()
                by_id = {r[0]: r for r in rows}
                return [
                    (by_id[rid][1], by_id[rid][2], score)
                    for rid, score in pairs
                    if rid in by_id
                ]
            finally:
                conn.close()
        except Exception as e:  # noqa: BLE001 — vec failure must never break search
            log.warning(
                "vec search failed for %s (%s); falling back to the in-memory scan",
                self.path, e,
            )
            self._vec_failed = True
            return None

    def search(self, query_vec: np.ndarray, k: int) -> list[tuple[str, float | None, float]]:
        """Top-k visual hits: `(file_path, frame_ts, score)` by cosine similarity,
        sorted by score desc. `frame_ts` is None for images, seconds for video frames.
        Returns one row per stored vector — a multi-keyframe video yields several rows;
        callers dedup to best-per-file as needed. Served by the vec0 backend when
        available (same ladder as `EmbeddingIndex.search`)."""
        vec_hits = self._vec_search(query_vec, k)
        if vec_hits is not None:
            return vec_hits
        paths, frame_ts, matrix = self.all_vectors()
        if not paths:
            return []
        scores = matrix @ query_vec.astype(np.float32, copy=False)
        k_eff = min(k, len(scores))
        if k_eff <= 0:
            return []
        top_idx = np.argpartition(-scores, k_eff - 1)[:k_eff]
        top_idx = top_idx[np.argsort(-scores[top_idx])]
        return [(paths[i], frame_ts[i], float(scores[i])) for i in top_idx]


def vector_backend_active(vault_root: Path) -> bool:
    """True when the vec0 backend would serve vector search for this vault now.

    Consults the same ladder `search()` uses (kill switch, load memo, per-instance
    sync state) without running a query. Warm-up branches on this: prime the vec
    tables when True, the in-memory matrix when False. Never creates the sidecar.
    """
    if vecstore.backend() == "numpy" or vecstore.load_failed():
        return False
    idx = get_embedding_index(vault_root)
    if idx._vec_failed or not idx.path.exists():
        return False
    conn = idx._connect()
    try:
        return _vec_gate(idx, conn)
    finally:
        conn.close()


def get_embedding_index(vault_root: Path) -> EmbeddingIndex:
    """Return the process-shared `EmbeddingIndex` for this vault.

    ALL production call sites (find, warm-up, writers, audit) must go through this
    so the in-memory matrix cache is shared and survives across calls — the whole
    reason find() stops paying a full reload per query. Tests may still construct
    `EmbeddingIndex` directly to exercise the class in isolation.
    """
    key = str(Path(vault_root).resolve())
    with _INDEX_CACHE_LOCK:
        idx = _INDEX_CACHE.get(key)
        if idx is None:
            idx = EmbeddingIndex(vault_root)
            _INDEX_CACHE[key] = idx
        return idx


def get_clip_index(vault_root: Path) -> ClipIndex:
    """Return the process-shared `ClipIndex` for this vault (see get_embedding_index)."""
    key = str(Path(vault_root).resolve())
    with _INDEX_CACHE_LOCK:
        idx = _CLIP_INDEX_CACHE.get(key)
        if idx is None:
            idx = ClipIndex(vault_root)
            _CLIP_INDEX_CACHE[key] = idx
        return idx


def clear_embedding_indexes() -> None:
    """Drop the shared index memo (and its in-memory matrices). Test hook — the
    per-test tmp vault would otherwise leave a stale instance keyed by its path."""
    with _INDEX_CACHE_LOCK:
        _INDEX_CACHE.clear()
        _CLIP_INDEX_CACHE.clear()


def upsert_after_write(vault_root: Path, written_paths: list[Path]) -> None:
    """Re-embed each markdown file in `written_paths` and refresh the sidecar.

    Soft no-op when sentence-transformers/torch aren't importable — keyword
    mode keeps working in stripped environments. Non-`.md` paths are skipped
    silently (writers pass log.md, index.md, etc. through here too).
    """
    global _IMPORT_FAILED
    if _IMPORT_FAILED:
        return
    # Test runs disable the heavy embedding path to keep the suite fast.
    # Production servers leave EXOMEM_DISABLE_EMBEDDINGS unset.
    if os.environ.get("EXOMEM_DISABLE_EMBEDDINGS"):
        return
    md_paths = [p for p in written_paths if _is_embeddable_path(p)]
    if not md_paths:
        return

    # While the background warm-up is loading the model, don't block this
    # write on the singleton lock — park the batch; the warm thread drains it
    # right after the model lands (readiness.mark_ready("embeddings")). If the
    # process dies before draining, audit/reconcile recover the stale sidecar.
    from . import readiness
    if readiness.defer("embeddings", (vault_root, tuple(md_paths))):
        log.info("write-embed deferred until the embedding model is warm (%d file(s))", len(md_paths))
        return

    try:
        get_model()  # triggers the heavy import; cheap thereafter.
    except ImportError as e:
        if not _IMPORT_FAILED:
            log.warning(
                "embeddings disabled (import failed: %s); writers will not "
                "update the vector sidecar. Keyword-mode find() still works.",
                e,
            )
            _IMPORT_FAILED = True
        return
    except Exception as e:
        log.warning("embedding model load failed: %s; skipping upsert", e)
        return

    from . import find as find_module

    index = get_embedding_index(vault_root)
    per_file: list[tuple[str, list[str], float]] = []
    for md in md_paths:
        try:
            mtime = md.stat().st_mtime
        except FileNotFoundError:
            # File was just written then disappeared — treat as a delete.
            try:
                rel = md.resolve().relative_to(vault_root.resolve()).as_posix()
                index.delete_file(rel)
            except ValueError:
                pass
            continue
        page = find_module._CACHE.get(md, vault_root)
        if page is None:
            continue
        chunks = _chunks_for_page(vault_root, page)
        if not chunks:
            # Page has no embeddable content — drop any stale rows for it.
            index.delete_file(page.rel_path)
            continue
        per_file.append((page.rel_path, chunks, mtime))

    if not per_file:
        return

    # Single batch encode across all files for throughput.
    flat: list[str] = []
    for _, chunks, _ in per_file:
        flat.extend(chunks)
    try:
        vectors = embed_texts(flat, is_query=False)
    except Exception as e:
        log.warning("embedding encode failed: %s; sidecar left stale", e)
        return

    offset = 0
    for rel_path, chunks, mtime in per_file:
        n = len(chunks)
        index.upsert_file(rel_path, chunks, vectors[offset:offset + n], mtime)
        offset += n

    # Claim-level sidecar (.claims.sqlite) rides the same write seam — opt-in via
    # EXOMEM_CLAIM_LEVEL, no-op otherwise. Local import avoids a module cycle
    # (claims imports embeddings at load; embeddings reaches claims only here, at
    # runtime). Best-effort: a claim-sidecar miss must never fail a vector write.
    try:
        from . import claims

        if claims.claim_level_enabled():
            claims.upsert_claims_after_write(vault_root, md_paths)
    except Exception as e:  # noqa: BLE001
        log.debug("claim sidecar upsert skipped (%s)", e)


def delete_after_remove(vault_root: Path, removed_rel_paths: list[str]) -> None:
    """Drop sidecar rows for files that were trashed. No-op if torch missing."""
    if _IMPORT_FAILED:
        return
    if os.environ.get("EXOMEM_DISABLE_EMBEDDINGS"):
        return
    if not removed_rel_paths:
        return
    try:
        index = get_embedding_index(vault_root)
    except Exception as e:
        log.warning("could not open embedding sidecar for delete: %s", e)
        return
    for rel in removed_rel_paths:
        try:
            index.delete_file(rel)
        except Exception as e:
            log.warning("delete_file(%s) failed in sidecar: %s", rel, e)


def index_incremental(
    vault_root: Path,
    *,
    batch_size: int = 256,
    dry_run: bool = False,
    log_fn=log.info,
) -> dict:
    """Incrementally (re)embed the markdown the index scope covers — idempotent, no wipe.

    Unlike `EmbeddingIndex.rebuild_all` (wipe-then-rebuild), this is the reconcile-
    style path the `exomem index` CLI drives: it SKIPS files whose sidecar rows are
    already current (stored `file_mtime` >= on-disk mtime, 1s slack for FS jitter),
    embeds only the new/changed ones in batches of ~`batch_size` chunks (progress
    logged between batches), and prunes rows for files that are gone or no longer
    indexable. Re-running after a clean pass embeds nothing.

    Scope is `index_scope()`: `"kb"` (default) or `"vault"`. Honors
    `access.is_indexable`, `_is_embeddable_path`, and `_chunks_for_page` — the same
    selection `rebuild_all` uses — so the two agree on WHICH files belong in the index.
    Returns a small stats dict (also the CLI's machine-readable output).
    """
    from . import access
    from . import find as find_module

    scope = index_scope()
    index = get_embedding_index(vault_root)
    row_mtimes = index.file_mtimes()

    pending: list[tuple[str, list[str], float]] = []
    seen_on_disk: set[str] = set()
    scanned = 0
    for md in _index_walk(vault_root):
        if not _is_embeddable_path(md):
            continue
        page = find_module._CACHE.get(md, vault_root)
        if page is None:
            continue
        if not access.is_indexable(vault_root, page.rel_path):
            continue  # excluded tree (_access.yaml) — keep it out of the index
        chunks = _chunks_for_page(vault_root, page)
        if not chunks:
            continue
        scanned += 1
        seen_on_disk.add(page.rel_path)
        prior = row_mtimes.get(page.rel_path)
        if prior is not None and page.mtime <= prior + 1.0:
            continue  # sidecar already current for this file
        pending.append((page.rel_path, chunks, page.mtime))

    # Rows for files no longer walked (deleted or newly excluded) → prune, so the
    # incremental index stays a faithful reflection of the scope without a full wipe.
    stale = [rp for rp in row_mtimes if rp not in seen_on_disk]

    stats = {
        "scope": scope,
        "scanned": scanned,
        "files_to_embed": len(pending),
        "chunks_embedded": 0,
        "files_pruned": len(stale),
        "dry_run": dry_run,
    }
    log_fn(
        f"index({scope}): {scanned} indexable file(s); {len(pending)} to (re)embed, "
        f"{len(stale)} stale row-set(s) to prune (dry_run={dry_run})"
    )
    if dry_run:
        return stats

    for rp in stale:
        index.delete_file(rp)

    total_files = len(pending)
    done_files = 0

    def _flush(group: list[tuple[str, list[str], float]]) -> None:
        nonlocal done_files
        if not group:
            return
        flat: list[str] = []
        for _rp, chs, _m in group:
            flat.extend(chs)
        vectors = embed_texts(flat, is_query=False)
        offset = 0
        for rp, chs, m in group:
            n = len(chs)
            index.upsert_file(rp, chs, vectors[offset:offset + n], m)
            offset += n
            done_files += 1
        stats["chunks_embedded"] += len(flat)
        log_fn(
            f"  …{done_files}/{total_files} file(s) embedded "
            f"({stats['chunks_embedded']} chunk(s))"
        )

    batch: list[tuple[str, list[str], float]] = []
    batch_chunks = 0
    for item in pending:
        batch.append(item)
        batch_chunks += len(item[1])
        if batch_chunks >= batch_size:
            _flush(batch)
            batch = []
            batch_chunks = 0
    _flush(batch)

    log_fn(f"index done: {stats}")
    return stats
