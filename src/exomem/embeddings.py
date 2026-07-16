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

import contextlib
import gc
import logging
import math
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from . import accel, index_paths, vecstore
from .clip_index import CLIP_DIM, ClipIndex
from .embedding_index import VECTOR_DIM, EmbeddingIndex
from .vector_index_common import vec_gate as _vec_gate

log = logging.getLogger(__name__)


MODEL_NAME = "BAAI/bge-base-en-v1.5"
# The cross-encoder reranker is a stateless scorer (no stored vectors / sidecar dim),
# so it can be swapped freely without a re-index. EXOMEM_RANKING_MODEL (legacy alias
# EXOMEM_RERANKER_MODEL) overrides; EXOMEM_DISABLE_RANKING turns it off entirely (a
# ~0.44 GB RAM + rerank-latency saving for low-resource / lite installs).
RERANKER_NAME = (
    os.environ.get("EXOMEM_RANKING_MODEL")
    or os.environ.get("EXOMEM_RERANKER_MODEL")
    or "BAAI/bge-reranker-base"
)
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
_MODEL = None
_MODEL_LOCK = threading.Lock()
_RERANKER = None
_RERANKER_LOCK = threading.Lock()
_CLIP_MODEL = None
_CLIP_LOCK = threading.Lock()
_IMPORT_FAILED = False  # one-time soft-fail flag for upsert_after_write
_CLIP_IMPORT_FAILED = False


class _ModelGuard:
    """In-flight + last-activity tracking for a lazily-loaded model singleton, so the
    idle-unload reaper (`model_reaper`) can reclaim it safely.

    Correctness rests on the worker holding a live LOCAL ref to the model for the whole
    encode (get_model() returns it, the worker calls model.encode on that local): nulling
    the module singleton can't collect it and `empty_cache()` frees only unused blocks, so
    an unload racing an in-flight encode is never a use-after-free. This guard therefore
    only prevents INEFFICIENCY — a transient double-load or empty_cache stealing reusable
    blocks mid-encode — via the in-flight counter, not a crash. Its lock guards only the
    counter + timestamp; it is never held across a model load or an encode.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self._lock = threading.Lock()
        self._inflight = 0
        self._last_activity = 0.0  # time.monotonic() of last load/encode; 0 = never loaded

    def touch(self) -> None:
        """Stamp activity (called on model load so a fresh model isn't instantly reaped)."""
        with self._lock:
            self._last_activity = time.monotonic()

    @contextlib.contextmanager
    def active(self):
        """Bracket an encode/predict: bump in-flight so the reaper skips this model."""
        with self._lock:
            self._inflight += 1
            self._last_activity = time.monotonic()
        try:
            yield
        finally:
            with self._lock:
                self._inflight -= 1
                self._last_activity = time.monotonic()

    def inflight(self) -> int:
        with self._lock:
            return self._inflight

    def last_activity(self) -> float:
        with self._lock:
            return self._last_activity


BGE_GUARD = _ModelGuard("embeddings")
RERANKER_GUARD = _ModelGuard("reranker")
CLIP_GUARD = _ModelGuard("clip")


def unload_model() -> bool:
    """Drop the bge singleton and release its GPU cache. Skips if a worker is mid-encode.

    Returns True if it actually unloaded. Safe under concurrent use (see `_ModelGuard`):
    a worker that already holds the model finishes its encode on its local ref. The busy
    check + null happen under `_MODEL_LOCK`, so no new get_model() can complete a load in
    between (get_model also takes `_MODEL_LOCK`)."""
    global _MODEL
    with _MODEL_LOCK:
        if _MODEL is None or BGE_GUARD.inflight() > 0:
            return False
        m, _MODEL = _MODEL, None
    del m
    gc.collect()
    accel.empty_cache()
    return True


def unload_reranker() -> bool:
    """Drop the reranker singleton + release GPU cache. See `unload_model`."""
    global _RERANKER
    with _RERANKER_LOCK:
        if _RERANKER is None or RERANKER_GUARD.inflight() > 0:
            return False
        m, _RERANKER = _RERANKER, None
    del m
    gc.collect()
    accel.empty_cache()
    return True


def unload_clip_model() -> bool:
    """Drop the CLIP singleton + release GPU cache. See `unload_model`."""
    global _CLIP_MODEL
    with _CLIP_LOCK:
        if _CLIP_MODEL is None or CLIP_GUARD.inflight() > 0:
            return False
        m, _CLIP_MODEL = _CLIP_MODEL, None
    del m
    gc.collect()
    accel.empty_cache()
    return True

# Process-lifetime memo of the per-vault index objects. Sharing ONE instance per
# vault is what makes the in-memory matrix cache survive across find() calls (and
# lets warm-up actually prime it) — previously every call site built a throwaway
# instance whose cache started empty, so all_vectors() paid a full O(vault) reload
# on every find. Keyed by the resolved vault path; guarded for the worker-thread
# pool + file-watcher/media-worker threads that touch these concurrently.
_INDEX_CACHE: dict[str, EmbeddingIndex] = {}
_CLIP_INDEX_CACHE: dict[str, ClipIndex] = {}
_INDEX_CACHE_LOCK = threading.Lock()


sidecar_path = index_paths.sidecar_path
clip_sidecar_path = index_paths.clip_sidecar_path


def clip_enabled() -> bool:
    """False when EXOMEM_DISABLE_CLIP is set (mirrors EXOMEM_DISABLE_EMBEDDINGS)."""
    return not os.environ.get("EXOMEM_DISABLE_CLIP")


def ranking_enabled() -> bool:
    """False when EXOMEM_DISABLE_RANKING is set — the reranker never loads or scores.

    The reranker is already opt-in per query (see find.should_rerank); this hard-off
    keeps it from ever being preloaded or invoked, freeing ~0.44 GB and its latency —
    the 'lite' knob for low-resource installs. Retrieval falls back to the fused
    vector+BM25 ordering, which is what serves the un-reranked majority of queries anyway.
    """
    return not os.environ.get("EXOMEM_DISABLE_RANKING")


INDEX_SCOPES = index_paths.INDEX_SCOPES
index_scope = index_paths.index_scope


def _index_walk(vault_root: Path):
    """Compatibility wrapper for the semantic-index path contract."""
    yield from index_paths.iter_index_markdown(vault_root)


def _is_embeddable_path(path: Path) -> bool:
    """Compatibility wrapper for derived-index markdown eligibility."""
    return index_paths.is_embeddable_path(path)


def get_model():
    """Lazy singleton selected by ``accel``, CPU-default unless explicitly set."""
    global _MODEL
    if _MODEL is not None:
        return _MODEL
    with _MODEL_LOCK:
        if _MODEL is not None:
            return _MODEL
        # Heavy import stays local — keyword-mode and existing tests must not
        # pay this cost.
        from sentence_transformers import SentenceTransformer

        device = accel.select_device(override_env="EXOMEM_EMBED_DEVICE")
        log.info("loading embedding model %s on %s", MODEL_NAME, device)
        _MODEL = _maybe_half(SentenceTransformer(MODEL_NAME, device=device), device)
    BGE_GUARD.touch()  # start the idle clock at load, not epoch 0
    return _MODEL


def get_reranker():
    """Lazy cross-encoder reranker sharing the configured text-path device."""
    global _RERANKER
    if _RERANKER is not None:
        return _RERANKER
    with _RERANKER_LOCK:
        if _RERANKER is not None:
            return _RERANKER
        from sentence_transformers import CrossEncoder

        device = accel.select_device(override_env="EXOMEM_EMBED_DEVICE")
        log.info("loading reranker %s on %s", RERANKER_NAME, device)
        _RERANKER = CrossEncoder(RERANKER_NAME, device=device)
    RERANKER_GUARD.touch()
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
    CLIP_GUARD.touch()
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
    with Image.open(path) as img, CLIP_GUARD.active():
        vec = model.encode(img.convert("RGB"), convert_to_numpy=True, normalize_embeddings=True)
    return vec.astype(np.float32, copy=False)


def embed_clip_text(query: str) -> np.ndarray:
    """Encode a text query into CLIP space → float32 (512,), L2-normalized."""
    try:
        model = get_clip_model()
    except ImportError as e:
        raise ClipUnavailable(f"sentence-transformers not installed: {e}") from e
    with CLIP_GUARD.active():
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
    with CLIP_GUARD.active():
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


_FALSY_ENV = {"", "0", "false", "no", "off"}


def scene_frames_enabled() -> bool:
    """EXOMEM_VIDEO_SCENE_FRAMES gates scene detection + persisted scene frames.

    Default OFF: video keyframe selection stays the uniform sampler and no frame
    files are written — byte-identical to the pre-feature behavior.
    """
    return os.environ.get("EXOMEM_VIDEO_SCENE_FRAMES", "").strip().lower() not in _FALSY_ENV


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
    with CLIP_GUARD.active():
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
    with CLIP_GUARD.active():
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
    with RERANKER_GUARD.active():
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
    with BGE_GUARD.active():
        vecs = model.encode(
            texts,
            batch_size=32,
            convert_to_numpy=True,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
    return vecs.astype(np.float32, copy=False)


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


def unload_index_caches() -> dict[str, int]:
    """Evict resident embedding/CLIP matrices from already-shared index objects."""
    with _INDEX_CACHE_LOCK:
        embedding_indexes = list(_INDEX_CACHE.values())
        clip_indexes = list(_CLIP_INDEX_CACHE.values())
    return {
        "embedding": sum(1 for idx in embedding_indexes if idx.unload_cache()),
        "clip": sum(1 for idx in clip_indexes if idx.unload_cache()),
    }


def _summarize_index_status(indexes: dict[str, object]) -> dict:
    by_vault = {key: idx.cache_status() for key, idx in indexes.items()}
    loaded = [s for s in by_vault.values() if s.get("loaded")]
    return {
        "indexes": len(by_vault),
        "loaded": len(loaded),
        "rows": sum(int(s.get("rows") or 0) for s in loaded),
        "bytes": sum(int(s.get("bytes") or 0) for s in loaded),
        "by_vault": by_vault,
    }


def index_cache_status() -> dict:
    """No-allocation residency status for already-created embedding index objects."""
    with _INDEX_CACHE_LOCK:
        embedding_indexes = dict(_INDEX_CACHE)
        clip_indexes = dict(_CLIP_INDEX_CACHE)
    return {
        "embedding": _summarize_index_status(embedding_indexes),
        "clip": _summarize_index_status(clip_indexes),
    }


@dataclass(frozen=True, slots=True)
class EmbeddingSyncStatus:
    """Bounded outcome from one embedding-sidecar dispatch.

    ``code`` is deliberately a stable enum-like value rather than an exception
    message.  Writer results can therefore report observed degradation without
    leaking backend, model, or filesystem details.
    """

    status: str
    code: str
    eligible_count: int

    def __post_init__(self) -> None:
        if type(self.status) is not str or self.status not in {
            "completed",
            "disabled",
            "deferred",
            "degraded",
        }:
            raise ValueError("unsupported embedding sync status")
        if type(self.code) is not str or not self.code or len(self.code) > 64:
            raise ValueError("embedding sync code must be bounded and nonempty")
        if type(self.eligible_count) is not int or self.eligible_count < 0:
            raise ValueError("embedding eligible_count must be nonnegative")

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "code": self.code,
            "eligible_count": self.eligible_count,
        }


def upsert_after_write_status(
    vault_root: Path, written_paths: list[Path]
) -> EmbeddingSyncStatus:
    """Re-embed eligible files and return an observable bounded outcome."""
    global _IMPORT_FAILED
    md_paths = [p for p in written_paths if index_paths.is_embeddable_path(p)]
    eligible_count = len(md_paths)
    # Test runs disable the heavy embedding path to keep the suite fast.
    # Production servers leave EXOMEM_DISABLE_EMBEDDINGS unset.
    if os.environ.get("EXOMEM_DISABLE_EMBEDDINGS"):
        return EmbeddingSyncStatus(
            "disabled", "embeddings_disabled", eligible_count
        )
    if not md_paths:
        return EmbeddingSyncStatus("completed", "no_eligible_paths", 0)
    if _IMPORT_FAILED:
        return EmbeddingSyncStatus(
            "disabled", "embeddings_import_unavailable", eligible_count
        )

    # While the background warm-up is loading the model, don't block this
    # write on the singleton lock — park the batch; the warm thread drains it
    # right after the model lands (readiness.mark_ready("embeddings")). If the
    # process dies before draining, audit/reconcile recover the stale sidecar.
    from . import readiness
    if readiness.defer("embeddings", (vault_root, tuple(md_paths))):
        log.info(
            "write-embed deferred until the embedding model is warm (%d file(s))",
            len(md_paths),
        )
        return EmbeddingSyncStatus("deferred", "deferred_warmup", eligible_count)

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
        return EmbeddingSyncStatus(
            "disabled", "embeddings_import_unavailable", eligible_count
        )
    except Exception as e:  # noqa: BLE001 - model backends soft-fail by contract
        log.warning("embedding model load failed: %s; skipping upsert", e)
        return EmbeddingSyncStatus(
            "degraded", "embedding_model_load_failed", eligible_count
        )

    from . import find as find_module

    try:
        index = get_embedding_index(vault_root)
    except Exception as e:  # noqa: BLE001 - derived index open is best-effort
        log.warning("could not open embedding sidecar for upsert: %s", e)
        return EmbeddingSyncStatus(
            "degraded", "embedding_index_open_failed", eligible_count
        )
    per_file: list[tuple[Path, Any, list[str], float]] = []
    failure_code: str | None = None
    for md in md_paths:
        try:
            mtime = md.stat().st_mtime
        except FileNotFoundError:
            # File was just written then disappeared — treat as a delete.
            try:
                rel = md.resolve().relative_to(vault_root.resolve()).as_posix()
                index.delete_file(rel)
            except ValueError:
                continue
            except Exception as e:  # noqa: BLE001 - derived delete is observable
                log.warning("embedding cleanup failed for disappeared file: %s", e)
                failure_code = "embedding_delete_failed"
            continue
        except OSError as e:
            log.warning("embedding input metadata could not be read: %s", e)
            failure_code = "embedding_input_unavailable"
            continue
        try:
            page = find_module._CACHE.get(md, vault_root)
        except Exception as e:  # noqa: BLE001 - parsing/cache failures are observable
            log.warning("embedding input could not be parsed: %s", e)
            failure_code = "embedding_input_unavailable"
            continue
        if page is None:
            failure_code = "embedding_input_unavailable"
            continue
        try:
            chunks = _chunks_for_page(vault_root, page)
        except Exception as e:  # noqa: BLE001 - chunk extraction is best-effort
            log.warning("embedding chunks could not be prepared: %s", e)
            failure_code = "embedding_chunking_failed"
            continue
        per_file.append((md, page, chunks, mtime))

    if not per_file:
        return EmbeddingSyncStatus(
            "completed" if failure_code is None else "degraded",
            failure_code or "embedding_upsert_completed",
            eligible_count,
        )

    from . import semantic_index

    for md, page, chunks, mtime in per_file:
        rel_path = page.rel_path
        if chunks:
            try:
                vectors = _embed_live_chunks(chunks)
                index.upsert_file(rel_path, chunks, vectors, mtime)
            except Exception as e:  # noqa: BLE001 - one bad encode must not fail the writer
                log.warning(
                    "embedding encode failed for %s: %s; sidecar left stale",
                    rel_path,
                    e,
                )
                failure_code = failure_code or "embedding_encode_failed"
        else:
            # Page has no embeddable chunks — drop stale page rows. Unit rows
            # are rebuilt independently below from the normalized unit parse.
            try:
                index.delete_file(rel_path)
            except Exception as e:  # noqa: BLE001 - stale-row cleanup is observable
                log.warning("embedding stale-row cleanup failed: %s", e)
                failure_code = failure_code or "embedding_delete_failed"

        try:
            state = semantic_index.current_parent_index_state(vault_root, md)
            units = [unit for unit in state.document.units if unit.unit_ref is not None]
            if units:
                unit_vectors = _embed_live_chunks([unit.content for unit in units])
                index.upsert_semantic_units(state, unit_vectors, mtime)
            else:
                index.delete_semantic_units(rel_path)
        except Exception as e:  # noqa: BLE001 - optional unit vectors soft-fail
            log.warning(
                "semantic-unit embedding update failed for %s: %s; sidecar left stale",
                rel_path,
                e,
            )
            failure_code = failure_code or "semantic_unit_embedding_encode_failed"

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
        failure_code = failure_code or "embedding_auxiliary_failed"
    return EmbeddingSyncStatus(
        "completed" if failure_code is None else "degraded",
        failure_code or "embedding_upsert_completed",
        eligible_count,
    )


def upsert_after_write(vault_root: Path, written_paths: list[Path]) -> bool:
    """Preserve the historical Boolean for primary vector work completion."""
    status = upsert_after_write_status(vault_root, written_paths)
    # Preserve the legacy memoized-import-failure precedence: historically a
    # stripped install returned ``False`` even for a batch containing no
    # embeddable paths.
    return not _IMPORT_FAILED and (
        status.status == "completed" or status.code == "embedding_auxiliary_failed"
    )


def _live_embed_max_chunks() -> int:
    try:
        return max(1, int(os.environ.get("EXOMEM_LIVE_EMBED_MAX_CHUNKS") or "256"))
    except ValueError:
        return 256


def _embed_live_chunks(chunks: list[str]) -> np.ndarray:
    """Encode one file in bounded slices while preserving chunk order."""
    limit = _live_embed_max_chunks()
    parts = [
        embed_texts(chunks[offset : offset + limit], is_query=False)
        for offset in range(0, len(chunks), limit)
    ]
    if not parts:
        return np.zeros((0, VECTOR_DIM), dtype=np.float32)
    if len(parts) == 1:
        return np.asarray(parts[0], dtype=np.float32)
    return np.concatenate(parts, axis=0)


def delete_after_remove_status(
    vault_root: Path, removed_rel_paths: list[str]
) -> EmbeddingSyncStatus:
    """Drop sidecar rows and return an observable bounded outcome."""
    eligible_count = len(removed_rel_paths)
    if os.environ.get("EXOMEM_DISABLE_EMBEDDINGS"):
        return EmbeddingSyncStatus(
            "disabled", "embeddings_disabled", eligible_count
        )
    if not removed_rel_paths:
        return EmbeddingSyncStatus("completed", "no_eligible_paths", 0)
    if _IMPORT_FAILED:
        return EmbeddingSyncStatus(
            "disabled", "embeddings_import_unavailable", eligible_count
        )
    try:
        index = get_embedding_index(vault_root)
    except Exception as e:  # noqa: BLE001 - derived index deletion is best-effort
        log.warning("could not open embedding sidecar for delete: %s", e)
        return EmbeddingSyncStatus(
            "degraded", "embedding_index_open_failed", eligible_count
        )
    succeeded = True
    for rel in removed_rel_paths:
        try:
            index.delete_file(rel)
        except Exception as e:  # noqa: BLE001 - derived index deletion is best-effort
            log.warning("delete_file(%s) failed in sidecar: %s", rel, e)
            succeeded = False
    return EmbeddingSyncStatus(
        "completed" if succeeded else "degraded",
        "embedding_delete_completed" if succeeded else "embedding_delete_failed",
        eligible_count,
    )


def delete_after_remove(vault_root: Path, removed_rel_paths: list[str]) -> None:
    """Compatibility wrapper preserving the legacy ``None`` return."""
    delete_after_remove_status(vault_root, removed_rel_paths)


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
    from . import access, semantic_index
    from . import find as find_module

    scope = index_paths.index_scope()
    index = get_embedding_index(vault_root)
    row_mtimes = index.file_mtimes()
    unit_parent_states = index.semantic_unit_parent_states()

    pending: list[tuple[str, list[str], float]] = []
    pending_units: list[tuple[semantic_index.SemanticParentIndexState, float]] = []
    seen_on_disk: set[str] = set()
    unit_seen_on_disk: set[str] = set()
    scanned = 0
    for md in index_paths.iter_index_markdown(vault_root):
        if not index_paths.is_embeddable_path(md):
            continue
        page = find_module._CACHE.get(md, vault_root)
        if page is None:
            continue
        if not access.is_indexable(vault_root, page.rel_path):
            continue  # excluded tree (_access.yaml) — keep it out of the index
        try:
            unit_state = semantic_index.build_parent_index_state(vault_root, md)
        except (OSError, UnicodeError, ValueError):
            unit_state = None
        if unit_state is not None:
            unit_seen_on_disk.add(page.rel_path)
            expected_refs = frozenset(
                unit.unit_ref
                for unit in unit_state.document.units
                if unit.unit_ref is not None
            )
            stored = unit_parent_states.get(page.rel_path)
            expected_stored = (
                frozenset({unit_state.parent_generation}),
                expected_refs,
            )
            if stored != expected_stored and (stored is not None or expected_refs):
                pending_units.append((unit_state, page.mtime))
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
    stale = sorted(
        {rp for rp in row_mtimes if rp not in seen_on_disk}
        | {rp for rp in unit_parent_states if rp not in unit_seen_on_disk}
    )

    stats = {
        "scope": scope,
        "scanned": scanned,
        "files_to_embed": len(pending),
        "chunks_embedded": 0,
        "unit_parents_to_embed": len(pending_units),
        "unit_vectors_embedded": 0,
        "files_pruned": len(stale),
        "dry_run": dry_run,
    }
    log_fn(
        f"index({scope}): {scanned} indexable file(s); {len(pending)} to (re)embed, "
        f"{len(pending_units)} semantic-unit parent(s) to (re)embed, "
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

    unit_batch: list[tuple[semantic_index.SemanticParentIndexState, float]] = []
    unit_batch_size = 0

    def _flush_units(
        group: list[tuple[semantic_index.SemanticParentIndexState, float]],
    ) -> None:
        if not group:
            return
        texts = [
            unit.content
            for state, _mtime in group
            for unit in state.document.units
            if unit.unit_ref is not None
        ]
        vectors = (
            embed_texts(texts, is_query=False)
            if texts
            else np.zeros((0, VECTOR_DIM), dtype=np.float32)
        )
        offset = 0
        for state, mtime in group:
            count = sum(
                unit.unit_ref is not None for unit in state.document.units
            )
            index.upsert_semantic_units(
                state,
                vectors[offset : offset + count],
                mtime,
            )
            offset += count
        stats["unit_vectors_embedded"] += len(texts)

    for item in pending_units:
        unit_batch.append(item)
        unit_batch_size += sum(
            unit.unit_ref is not None for unit in item[0].document.units
        )
        if unit_batch_size >= batch_size:
            _flush_units(unit_batch)
            unit_batch = []
            unit_batch_size = 0
    _flush_units(unit_batch)

    log_fn(f"index done: {stats}")
    return stats
