"""Zero-shot image tagging via the already-loaded CLIP model (EXOMEM_IMAGE_TAGS, default OFF).

Enriches an image's per-file MEASUREMENT: score the image's CLIP embedding against a fixed,
generic tag vocabulary by cosine, take the top-K tags above a threshold, and (at the
extraction seam) append them to the image's extracted text so they become BM25-findable and
bge-embedded. This makes a photo discoverable by what it depicts ("invoice", "whiteboard",
"screenshot") even when it carries no on-image text.

PURE-SUBSTRATE NOTE: CLIP zero-shot scoring is deterministic MEASUREMENT — the same class as
Tesseract OCR, CLIP visual search, and the bge embedder. It maps pixels to a fixed cosine
score against frozen text vectors; it does not generate language and is not a reasoning LLM.
Cross-image inference stays Claude's job.

Reuses `embeddings.get_clip_model()` (one model that encodes BOTH images and text) and its
device logic (the whisper-cuDNN-shadow CPU fallback in `embeddings._clip_device()`) — no new
dependency, no second model. The vocabulary is embedded ONCE with CLIP's text encoder and
cached for the process. Soft-fail everywhere: CLIP/Pillow/model absent, or any error, yields
no tags and leaves extraction output unchanged. Default-OFF ⇒ byte-identical when unset.

Tunables (env): `EXOMEM_IMAGE_TAGS` (gate), `EXOMEM_IMAGE_TAGS_TOPK` (default 5),
`EXOMEM_IMAGE_TAGS_THRESHOLD` (raw cosine floor, default 0.22).
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

import numpy as np

from . import embeddings

log = logging.getLogger(__name__)


# A fixed, GENERIC visual-concept vocabulary (objects, scenes, document/screen kinds). Kept
# brand- and identity-free on purpose: this ships under src/exomem/ and is leak-guarded
# (tests/test_scaffold_no_leak.py). Lowercase single concepts; embedded once with CLIP's text
# encoder. Order is irrelevant — tags are emitted by descending cosine, not vocab position.
TAG_VOCAB: tuple[str, ...] = (
    # documents & paperwork
    "invoice", "receipt", "contract", "form", "spreadsheet", "report", "resume",
    "letter", "certificate", "ticket", "menu", "label", "business card", "sticky note",
    "handwriting", "signature", "stamp", "barcode", "qr code", "calendar", "schedule",
    "map", "blueprint", "sheet music", "newspaper", "magazine", "book", "document",
    "poster", "flyer", "brochure",
    # data visualization & diagrams
    "table", "chart", "graph", "diagram", "flowchart", "infographic", "timeline",
    "mind map", "whiteboard", "presentation slide",
    # screens, software & technology
    "screenshot", "dashboard", "website", "app interface", "login screen",
    "error message", "terminal", "source code", "email", "chat conversation",
    "social media post", "video call", "keyboard", "monitor", "laptop", "smartphone",
    "tablet", "computer", "server rack", "circuit board", "robot", "drone", "camera",
    "headphones",
    # buildings & places
    "cityscape", "skyline", "street", "road", "highway", "bridge", "building",
    "skyscraper", "house", "apartment", "office", "kitchen", "bathroom", "bedroom",
    "living room", "classroom", "library", "store", "restaurant", "cafe", "hospital",
    "church", "museum", "stadium", "airport", "train station", "parking lot",
    "construction site", "factory", "warehouse", "farm",
    # nature & landscape
    "garden", "park", "playground", "beach", "ocean", "lake", "river", "waterfall",
    "mountain", "valley", "forest", "desert", "field", "cave", "island", "snow",
    "glacier", "sky", "clouds", "sunset", "sunrise", "rainbow", "storm", "lightning",
    "rain", "fog", "fire", "smoke", "night sky", "stars", "moon",
    # people & activities
    "person", "crowd", "portrait", "selfie", "group photo", "family", "child", "baby",
    "wedding", "party", "concert", "meeting", "conference", "sports", "running",
    "cycling", "swimming", "hiking", "dancing", "cooking", "reading", "gaming",
    "shopping", "traveling", "protest", "parade", "graduation",
    # animals
    "dog", "cat", "bird", "horse", "cow", "sheep", "fish", "insect", "butterfly",
    "wildlife",
    # food & drink
    "food", "meal", "breakfast", "coffee", "tea", "cake", "dessert", "fruit",
    "vegetable", "drink", "wine", "cocktail",
    # vehicles
    "car", "truck", "bus", "train", "airplane", "boat", "ship", "bicycle",
    "motorcycle",
    # objects
    "money", "coin", "jewelry", "watch", "clock", "flower", "plant", "tree", "tool",
    "machine", "furniture", "chair", "bed", "lamp", "sculpture", "statue", "sign",
    "billboard", "flag", "box", "bottle", "bag", "clothing", "shoes", "hat", "toy",
    "ball", "guitar", "piano",
    # art, media & imaging style
    "photograph", "illustration", "cartoon", "comic", "sketch", "logo", "icon", "meme",
    "abstract art", "pattern", "texture", "collage", "black and white photo",
    "aerial view", "close-up", "x-ray", "medical scan", "microscope image",
)

DEFAULT_TOP_K = 5
DEFAULT_THRESHOLD = 0.22

_VOCAB_MATRIX: np.ndarray | None = None
_VOCAB_LOCK = threading.Lock()


def image_tags_enabled() -> bool:
    """True only when EXOMEM_IMAGE_TAGS is set. OFF by default — tagging never changes
    extraction output unless explicitly opted in (so output is byte-identical when unset)."""
    return bool(os.environ.get("EXOMEM_IMAGE_TAGS"))


def _top_k() -> int:
    """Max tags to emit (EXOMEM_IMAGE_TAGS_TOPK, default DEFAULT_TOP_K)."""
    raw = os.environ.get("EXOMEM_IMAGE_TAGS_TOPK")
    if raw:
        try:
            v = int(raw)
            if v > 0:
                return v
        except ValueError:
            pass
    return DEFAULT_TOP_K


def _threshold() -> float:
    """Raw-cosine floor a tag must clear (EXOMEM_IMAGE_TAGS_THRESHOLD, default DEFAULT_THRESHOLD)."""
    raw = os.environ.get("EXOMEM_IMAGE_TAGS_THRESHOLD")
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return DEFAULT_THRESHOLD


def _vocab_matrix() -> np.ndarray:
    """Cached CLIP text-embedding matrix of TAG_VOCAB → `(len(TAG_VOCAB), CLIP_DIM)`.

    Embedded once per process via CLIP's text encoder. Not cached on failure: an error
    propagates so `compute_tags` soft-fails and a later call can retry once CLIP is present.
    """
    global _VOCAB_MATRIX
    if _VOCAB_MATRIX is not None:
        return _VOCAB_MATRIX
    with _VOCAB_LOCK:
        if _VOCAB_MATRIX is None:
            _VOCAB_MATRIX = embeddings.embed_clip_texts(list(TAG_VOCAB))
    return _VOCAB_MATRIX


def compute_tags(path: str | Path) -> list[str]:
    """Top-K vocab tags (descending cosine) whose score clears the threshold, or [] on soft-fail.

    Embeds the image with CLIP, cosines it against the cached vocabulary matrix, and returns the
    matching tags. Never raises: a missing dep/model/Pillow, an unreadable image, or any error
    returns [] so the caller keeps the unchanged extraction text.
    """
    p = Path(path)
    try:
        img_vec = embeddings.embed_image(p)
        mat = _vocab_matrix()
    except embeddings.ClipUnavailable as e:
        log.debug("image-tags: CLIP unavailable for %s: %s", p.name, e)
        return []
    except Exception:  # noqa: BLE001 — tagging is best-effort; never break extraction
        log.warning("image-tags: tagging failed for %s; no tags applied", p.name, exc_info=True)
        return []
    if mat.size == 0:
        return []
    scores = mat @ img_vec.astype(np.float32, copy=False)
    threshold = _threshold()
    top_k = _top_k()
    order = np.argsort(-scores)[:top_k]
    return [TAG_VOCAB[i] for i in order if scores[i] >= threshold]


def format_tags_line(tags: list[str]) -> str:
    """Render tags as the single indexed line appended to an image's text, e.g.
    `Tags: invoice, table, screenshot`. Empty string for no tags (caller skips appending)."""
    return "Tags: " + ", ".join(tags) if tags else ""
