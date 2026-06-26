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
import sqlite3
import threading
from pathlib import Path

import numpy as np


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


def sidecar_path(vault_root: Path) -> Path:
    return vault_root / "Knowledge Base" / ".embeddings.sqlite"


def clip_sidecar_path(vault_root: Path) -> Path:
    """Separate per-machine sidecar for CLIP image vectors (independent lifecycle)."""
    return vault_root / "Knowledge Base" / ".clip.sqlite"


def clip_enabled() -> bool:
    """False when KB_MCP_DISABLE_CLIP is set (mirrors KB_MCP_DISABLE_EMBEDDINGS)."""
    return not os.environ.get("KB_MCP_DISABLE_CLIP")


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
    """Lazy singleton. Picks CUDA when available, falls back to CPU."""
    global _MODEL
    if _MODEL is not None:
        return _MODEL
    with _MODEL_LOCK:
        if _MODEL is not None:
            return _MODEL
        # Heavy imports stay local — keyword-mode and existing tests must not
        # pay this cost.
        import torch
        from sentence_transformers import SentenceTransformer

        device = "cuda" if torch.cuda.is_available() else "cpu"
        log.info("loading embedding model %s on %s", MODEL_NAME, device)
        _MODEL = SentenceTransformer(MODEL_NAME, device=device)
    return _MODEL


def get_reranker():
    """Lazy singleton for the cross-encoder reranker. CUDA when available."""
    global _RERANKER
    if _RERANKER is not None:
        return _RERANKER
    with _RERANKER_LOCK:
        if _RERANKER is not None:
            return _RERANKER
        import torch
        from sentence_transformers import CrossEncoder

        device = "cuda" if torch.cuda.is_available() else "cpu"
        log.info("loading reranker %s on %s", RERANKER_NAME, device)
        _RERANKER = CrossEncoder(RERANKER_NAME, device=device)
    return _RERANKER


class ClipUnavailable(Exception):
    """CLIP (sentence-transformers/Pillow) isn't importable — soft-fail signal."""


def _clip_device() -> str:
    """Device for CLIP. Honors KB_MCP_CLIP_DEVICE; otherwise GPU only when ASR is NOT
    running in this process, else CPU.

    Why not always GPU: faster-whisper's CUDA-12 cuDNN/cuBLAS wheels get PATH-prepended
    (extract._ensure_cuda_dll_path) so ctranslate2 can load — which then shadows
    torch-cu132's bundled cuDNN 13 and makes CLIP's ViT Conv2d die with
    CUDNN_STATUS_SUBLIBRARY_VERSION_MISMATCH. bge survives (pure transformer, no conv);
    CLIP's vision tower doesn't. Since the media worker prewarms ASR at startup, having
    extraction enabled means PATH is already poisoned, so CLIP must run on CPU — a tiny
    ViT-B/32, off the request path, embeds in well under a second. A CLIP-only box
    (KB_MCP_DISABLE_MEDIA_EXTRACTION set) has no conflict and keeps the GPU.
    """
    override = os.environ.get("KB_MCP_CLIP_DEVICE")
    if override:
        return override
    # Mirror extract.extraction_enabled() without importing it (avoids a new module edge).
    asr_active = not os.environ.get("KB_MCP_DISABLE_MEDIA_EXTRACTION")
    if asr_active:
        return "cpu"
    try:
        import torch

        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:  # noqa: BLE001 — torch absent on a lean box → CPU
        return "cpu"


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
        _CLIP_MODEL = SentenceTransformer(CLIP_MODEL_NAME, device=device)
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


CLIP_VIDEO_FRAMES = 8  # frame budget for the unknown-duration sequential fallback


# --- Per-keyframe multi-vector video sampling -------------------------------
# One mean-pooled vector blurs a long/multi-scene video. Instead we keep N
# per-keyframe vectors so a video is findable at the SPECIFIC moment. The
# sampler is duration-scaled seek-sampling (O(1) in length, no full decode) +
# perceptual-hash near-dup suppression (collapses static talking-head runs),
# capped to bound storage. No new dependency — PIL + numpy only.
MAX_VIDEO_KEYFRAMES = 40  # hard cap on vectors per video (KB_MCP_MAX_VIDEO_KEYFRAMES overrides)
MIN_VIDEO_KEYFRAMES = 4
VIDEO_CANDIDATE_INTERVAL_SECS = 8  # ≈ one candidate keyframe per this many seconds
PHASH_DEDUP_DISTANCE = 5  # Hamming distance under which two frames count as near-dups


def _max_video_keyframes() -> int:
    raw = os.environ.get("KB_MCP_MAX_VIDEO_KEYFRAMES")
    if raw:
        try:
            v = int(raw)
            if v > 0:
                return v
        except ValueError:
            pass
    return MAX_VIDEO_KEYFRAMES


def _avg_hash(img, *, size: int = 8) -> int:
    """64-bit perceptual average-hash: downscale → grayscale → bit per pixel vs mean.

    Keys on luminance *structure*, so it distinguishes textured frames (faces, slides,
    whiteboards — what real recordings contain) but is blind to two flat frames of
    differing uniform colour (both hash to all-ones). Dedup is best-effort; a
    pathologically flat video simply keeps fewer keyframes.
    """
    small = img.convert("L").resize((size, size))
    arr = np.asarray(small, dtype=np.float32).ravel()
    bits = arr >= arr.mean()
    val = 0
    for b in bits:
        val = (val << 1) | int(b)
    return val


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


def embed_video_frames(path: Path) -> list[tuple[float, np.ndarray]]:
    """Encode a video → `[(timestamp_seconds, CLIP vector)]`, one per keyframe.

    Multi-vector replacement for `embed_video`'s single mean-pooled vector: scene-aware
    (duration-scaled seek-sampling + perceptual-hash near-dup suppression, capped at
    `MAX_VIDEO_KEYFRAMES`) so a long/multi-scene video is findable at the SPECIFIC moment.
    Each vector is 512-d, L2-normalized. Raises ClipUnavailable if CLIP/PyAV/Pillow are
    missing or no frame decodes.
    """
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


class EmbeddingIndex:
    """Per-vault sqlite sidecar holding chunk-level vectors.

    The matrix returned by `all_vectors()` is cached per-process and
    invalidated by the sidecar's own mtime — any writer that ran
    `upsert_file()` advances the mtime, so the next search call reloads.
    """

    def __init__(self, vault_root: Path):
        self.vault_root = vault_root
        self.path = sidecar_path(vault_root)
        self._cache: tuple[float, list[tuple[str, int, str]], np.ndarray] | None = None

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
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
            with conn:
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
        finally:
            conn.close()
        self._cache = None  # invalidate in-memory matrix

    def delete_file(self, rel_path: str) -> None:
        conn = self._connect()
        try:
            with conn:
                conn.execute("DELETE FROM chunks WHERE file_path = ?", (rel_path,))
        finally:
            conn.close()
        self._cache = None

    def all_vectors(self) -> tuple[list[tuple[str, int, str]], np.ndarray]:
        """Return `(metadata, matrix)` cached until the sidecar mtime advances.

        metadata[i] = (file_path, chunk_idx, chunk_text); matrix[i] = vector.
        """
        try:
            sidecar_mtime = self.path.stat().st_mtime
        except FileNotFoundError:
            return [], np.zeros((0, VECTOR_DIM), dtype=np.float32)
        if self._cache and self._cache[0] == sidecar_mtime:
            return self._cache[1], self._cache[2]
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT file_path, chunk_idx, chunk_text, vector FROM chunks "
                "ORDER BY file_path, chunk_idx"
            ).fetchall()
        finally:
            conn.close()
        if not rows:
            self._cache = (sidecar_mtime, [], np.zeros((0, VECTOR_DIM), dtype=np.float32))
            return self._cache[1], self._cache[2]
        metadata: list[tuple[str, int, str]] = []
        vectors: list[np.ndarray] = []
        for fp, idx, txt, blob in rows:
            metadata.append((fp, idx, txt))
            vectors.append(np.frombuffer(blob, dtype=np.float32))
        matrix = np.stack(vectors, axis=0)
        self._cache = (sidecar_mtime, metadata, matrix)
        return metadata, matrix

    def search(
        self, query_vec: np.ndarray, k: int
    ) -> list[tuple[str, int, str, float]]:
        """Top-k chunk hits: list of `(file_path, chunk_idx, chunk_text, score)`."""
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
        return [
            (metadata[i][0], metadata[i][1], metadata[i][2], float(scores[i]))
            for i in top_idx
        ]

    def rebuild_all(self) -> int:
        """Wipe + re-embed every compiled .md under Knowledge Base/. Returns row count."""
        from . import access
        from . import find as find_module

        kb = self.vault_root / "Knowledge Base"
        if not kb.is_dir():
            return 0
        # Wipe whole table — easier than per-file diff for a one-shot rebuild.
        conn = self._connect()
        try:
            with conn:
                conn.execute("DELETE FROM chunks")
        finally:
            conn.close()
        self._cache = None

        all_chunks: list[tuple[str, list[str], float]] = []
        for md in find_module._walk_md(kb):
            if not _is_embeddable_path(md):
                continue
            page = find_module._CACHE.get(md, self.vault_root)
            if page is None:
                continue
            if not access.is_indexable(self.vault_root, page.rel_path):
                continue  # excluded tree (_access.yaml) — keep it out of the index
            chunks = chunk_text(page.title, page.body)
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

        # Slice back per-file and write.
        offset = 0
        total = 0
        for rel_path, chunks, mtime in all_chunks:
            n = len(chunks)
            self.upsert_file(rel_path, chunks, vectors[offset:offset + n], mtime)
            offset += n
            total += n
        return total


class ClipIndex:
    """Per-vault sqlite sidecar of CLIP image vectors — one vector per image.

    Mirrors EmbeddingIndex (mtime-cached matrix, cosine search) but keyed by image
    file with no chunking. Lives in its own `.clip.sqlite` so the bge text index is
    untouched and the two evolve independently.
    """

    def __init__(self, vault_root: Path):
        self.vault_root = vault_root
        self.path = clip_sidecar_path(vault_root)
        # (sidecar_mtime, file_paths, frame_ts_list, matrix) — frame_ts is None for images.
        self._cache: tuple[float, list[str], list[float | None], np.ndarray] | None = None

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
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
            with conn:
                conn.execute(
                    "DELETE FROM images WHERE file_path = ? AND frame_ts IS NULL",
                    (rel_path,),
                )
                conn.execute(
                    "INSERT INTO images (file_path, frame_ts, vector, file_mtime) "
                    "VALUES (?, NULL, ?, ?)",
                    (rel_path, vector.astype(np.float32).tobytes(), mtime),
                )
        finally:
            conn.close()
        self._cache = None

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
            with conn:
                conn.execute("DELETE FROM images WHERE file_path = ?", (rel_path,))
                conn.executemany(
                    "INSERT INTO images (file_path, frame_ts, vector, file_mtime) "
                    "VALUES (?, ?, ?, ?)",
                    [
                        (rel_path, float(ts), vec.astype(np.float32).tobytes(), mtime)
                        for ts, vec in frames
                    ],
                )
        finally:
            conn.close()
        self._cache = None

    def delete(self, rel_path: str) -> None:
        conn = self._connect()
        try:
            with conn:
                conn.execute("DELETE FROM images WHERE file_path = ?", (rel_path,))
        finally:
            conn.close()
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
        """Returns parallel `(file_paths, frame_ts_list, matrix)`. frame_ts is None
        for image rows, a float (seconds) for video keyframe rows."""
        try:
            sidecar_mtime = self.path.stat().st_mtime
        except FileNotFoundError:
            return [], [], np.zeros((0, CLIP_DIM), dtype=np.float32)
        if self._cache and self._cache[0] == sidecar_mtime:
            return self._cache[1], self._cache[2], self._cache[3]
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT file_path, frame_ts, vector FROM images ORDER BY file_path, frame_ts"
            ).fetchall()
        finally:
            conn.close()
        if not rows:
            self._cache = (sidecar_mtime, [], [], np.zeros((0, CLIP_DIM), dtype=np.float32))
            return self._cache[1], self._cache[2], self._cache[3]
        paths = [r[0] for r in rows]
        frame_ts = [r[1] for r in rows]
        matrix = np.stack([np.frombuffer(r[2], dtype=np.float32) for r in rows], axis=0)
        self._cache = (sidecar_mtime, paths, frame_ts, matrix)
        return paths, frame_ts, matrix

    def search(self, query_vec: np.ndarray, k: int) -> list[tuple[str, float | None, float]]:
        """Top-k visual hits: `(file_path, frame_ts, score)` by cosine similarity,
        sorted by score desc. `frame_ts` is None for images, seconds for video frames.
        Returns one row per stored vector — a multi-keyframe video yields several rows;
        callers dedup to best-per-file as needed."""
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
    # Production servers leave KB_MCP_DISABLE_EMBEDDINGS unset.
    if os.environ.get("KB_MCP_DISABLE_EMBEDDINGS"):
        return
    md_paths = [p for p in written_paths if _is_embeddable_path(p)]
    if not md_paths:
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

    index = EmbeddingIndex(vault_root)
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
        chunks = chunk_text(page.title, page.body)
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


def delete_after_remove(vault_root: Path, removed_rel_paths: list[str]) -> None:
    """Drop sidecar rows for files that were trashed. No-op if torch missing."""
    if _IMPORT_FAILED:
        return
    if os.environ.get("KB_MCP_DISABLE_EMBEDDINGS"):
        return
    if not removed_rel_paths:
        return
    try:
        index = EmbeddingIndex(vault_root)
    except Exception as e:
        log.warning("could not open embedding sidecar for delete: %s", e)
        return
    for rel in removed_rel_paths:
        try:
            index.delete_file(rel)
        except Exception as e:
            log.warning("delete_file(%s) failed in sidecar: %s", rel, e)
