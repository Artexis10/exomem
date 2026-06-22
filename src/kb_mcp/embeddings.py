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


def get_clip_model():
    """Lazy CLIP singleton (encodes BOTH images and text). CUDA when available."""
    global _CLIP_MODEL
    if _CLIP_MODEL is not None:
        return _CLIP_MODEL
    with _CLIP_LOCK:
        if _CLIP_MODEL is not None:
            return _CLIP_MODEL
        import torch
        from sentence_transformers import SentenceTransformer

        device = "cuda" if torch.cuda.is_available() else "cpu"
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
        self._cache: tuple[float, list[str], np.ndarray] | None = None

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS images (
                file_path TEXT PRIMARY KEY,
                vector BLOB NOT NULL,
                file_mtime REAL NOT NULL
            )
            """
        )
        return conn

    def upsert(self, rel_path: str, vector: np.ndarray, mtime: float) -> None:
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO images (file_path, vector, file_mtime) VALUES (?, ?, ?)",
                    (rel_path, vector.astype(np.float32).tobytes(), mtime),
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
        """True if this image already has a vector (used by the startup scan)."""
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

    def all_vectors(self) -> tuple[list[str], np.ndarray]:
        try:
            sidecar_mtime = self.path.stat().st_mtime
        except FileNotFoundError:
            return [], np.zeros((0, CLIP_DIM), dtype=np.float32)
        if self._cache and self._cache[0] == sidecar_mtime:
            return self._cache[1], self._cache[2]
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT file_path, vector FROM images ORDER BY file_path"
            ).fetchall()
        finally:
            conn.close()
        if not rows:
            self._cache = (sidecar_mtime, [], np.zeros((0, CLIP_DIM), dtype=np.float32))
            return self._cache[1], self._cache[2]
        paths = [r[0] for r in rows]
        matrix = np.stack([np.frombuffer(r[1], dtype=np.float32) for r in rows], axis=0)
        self._cache = (sidecar_mtime, paths, matrix)
        return paths, matrix

    def search(self, query_vec: np.ndarray, k: int) -> list[tuple[str, float]]:
        """Top-k image hits: list of `(file_path, score)` by cosine similarity."""
        paths, matrix = self.all_vectors()
        if not paths:
            return []
        scores = matrix @ query_vec.astype(np.float32, copy=False)
        k_eff = min(k, len(scores))
        if k_eff <= 0:
            return []
        top_idx = np.argpartition(-scores, k_eff - 1)[:k_eff]
        top_idx = top_idx[np.argsort(-scores[top_idx])]
        return [(paths[i], float(scores[i])) for i in top_idx]


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
