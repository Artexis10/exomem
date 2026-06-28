"""Server-side media extraction — deterministic modality→text (transduction, not a brain).

ASR (audio/video via faster-whisper), OCR (images via Tesseract), and embedded-text
(PDF via PyMuPDF, with a rasterize+OCR fallback for scanned pages). This is "measure,"
not "reason" — the same category as the bge-base embedder already running on the GPU;
Claude still does all the thinking. The extracted text feeds the Evidence sidecar so an
otherwise-opaque binary becomes findable by its content.

Each engine is **soft-imported** and lazily loaded: a lean box without the `[media]`
extra (or with `KB_MCP_DISABLE_MEDIA_EXTRACTION` set) simply raises `ExtractionUnavailable`,
and the caller skips server extraction — the model-driven `/upload` `text` path still works.

Engines are swappable behind `extract_text(path, media_type=...) -> ExtractResult`: the
faster-whisper backend can be replaced with a torch/transformers Whisper if CTranslate2
lacks Blackwell `sm_120` kernels (the verification gate), without touching callers.

Two OPTIONAL deepen-the-moat transducers ship here, both DEFAULT-OFF + soft-fail:

- ASR speaker diarization (`KB_MCP_DIARIZE`): a pretrained clustering model
  (pyannote.audio) labels who-spoke-when and prefixes transcript turns with
  `[Speaker A]: …`. Deterministic (a frozen clustering model, not an LLM), so it is
  pure-substrate "measure" — but it stays off by default and falls back to the plain
  transcript when the dep/model isn't present, so existing extraction is unchanged.
- Vision captioning (`KB_MCP_VISION_CAPTION`): a FROZEN image-caption model
  (BLIP/Florence-2 class) prepends a one-line description to an image's OCR text so a
  photo with no text is still findable. A frozen caption model is deterministic
  transduction — the same class as Tesseract/CLIP/bge — NOT a reasoning LLM. But
  because it *generates* text it ships OFF by default; flip the flag to opt in once
  you've confirmed the model is the frozen captioner you intend. Soft-fails to
  OCR-only when the dep/model/GPU is absent.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

# Media-type buckets by extension. Extension-based is deliberate: no libmagic dep, and
# the uploader names the file. Unknown extension → not extractable (returns None).
_AUDIO_EXTS = frozenset({".mp3", ".wav", ".m4a", ".flac", ".ogg", ".oga", ".aac", ".wma", ".opus"})
_VIDEO_EXTS = frozenset({".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".wmv", ".flv", ".mpeg", ".mpg"})
_IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".tif", ".webp", ".heic"})
_PDF_EXTS = frozenset({".pdf"})
# Documents → MarkItDown (Microsoft, MIT) renders office/html to markdown, fully local.
# PDF deliberately stays on PyMuPDF (markitdown's PDF path is its weakest). The rest are
# tiny native parsers — no dependency. Only formats the vault actually holds.
_DOC_EXTS: dict[str, str] = {
    ".docx": "docx", ".xlsx": "xlsx", ".pptx": "pptx", ".html": "html", ".htm": "html",
}
_MARKITDOWN_KINDS = frozenset(_DOC_EXTS.values())  # {"docx", "xlsx", "pptx", "html"}
_TEXT_EXTS = frozenset({".txt", ".text", ".log"})  # plain UTF-8 read
_EMAIL_EXTS = frozenset({".eml"})                  # stdlib email parser
_CAL_EXTS = frozenset({".ics"})                    # native VEVENT parse

WHISPER_MODEL = os.environ.get("KB_MCP_WHISPER_MODEL", "large-v3")
# A PDF page yielding fewer than this many characters of embedded text is treated as
# scanned → rasterize + OCR fallback.
_PDF_OCR_MIN_CHARS = 16


@dataclass
class ExtractResult:
    text: str
    media_type: str           # audio|video|image|pdf|docx|xlsx|pptx|html|text|email|calendar
    engine: str               # provenance, e.g. "faster-whisper:large-v3", "tesseract", "pymupdf"
    warnings: list[str] = field(default_factory=list)
    # Optional speaker-diarized turns from ASR (KB_MCP_DIARIZE). Each entry is
    # `{"speaker": "Speaker A", "start": float, "end": float, "text": str}`. Default
    # None so every existing call site and engine is unchanged; only set when
    # diarization is enabled AND succeeds (else the plain transcript flows through).
    speakers: list[dict] | None = None


class ExtractionUnavailable(Exception):
    """No engine is installed/importable for this media type (soft-fail signal)."""


# ---------------- public API ----------------


def media_type_for(path: str | Path) -> str | None:
    """Coarse extraction kind for a path's extension, else None (not extractable).

    audio/video → ASR, image → OCR (+CLIP), pdf → PyMuPDF, docx/xlsx/pptx/html →
    MarkItDown, text → plain read, email → stdlib parse, calendar → VEVENT parse.
    """
    ext = Path(path).suffix.lower()
    if ext in _AUDIO_EXTS:
        return "audio"
    if ext in _VIDEO_EXTS:
        return "video"
    if ext in _IMAGE_EXTS:
        return "image"
    if ext in _PDF_EXTS:
        return "pdf"
    if ext in _DOC_EXTS:
        return _DOC_EXTS[ext]
    if ext in _TEXT_EXTS:
        return "text"
    if ext in _EMAIL_EXTS:
        return "email"
    if ext in _CAL_EXTS:
        return "calendar"
    return None


def is_extractable(path: str | Path) -> bool:
    return media_type_for(path) is not None


def extraction_enabled() -> bool:
    """False when KB_MCP_DISABLE_MEDIA_EXTRACTION is set (mirrors the embeddings flag)."""
    return not os.environ.get("KB_MCP_DISABLE_MEDIA_EXTRACTION")


def extract_text(path: str | Path, *, media_type: str | None = None) -> ExtractResult:
    """Extract text from a media file. Raises ExtractionUnavailable if no engine fits/installed."""
    p = Path(path)
    mt = media_type or media_type_for(p)
    if mt in ("audio", "video"):
        return _transcribe(p, mt)
    if mt == "image":
        return _ocr_image(p)
    if mt == "pdf":
        return _extract_pdf(p)
    if mt in _MARKITDOWN_KINDS:
        return _extract_document(p, mt)
    if mt == "text":
        return _extract_textfile(p)
    if mt == "email":
        return _extract_eml(p)
    if mt == "calendar":
        return _extract_ics(p)
    raise ExtractionUnavailable(f"no extractor for media_type={mt!r} (path {p.name!r})")


# ---------------- engines (lazy singletons, soft-imported) ----------------

_WHISPER = None  # faster-whisper WhisperModel singleton
_CUDA_DLL_PATH_DONE = False


def _ensure_cuda_dll_path() -> None:
    """Register the nvidia-* wheel bin dirs on Windows' DLL search path.

    ctranslate2 (faster-whisper's backend) is built against CUDA 12 and loads
    `cublas64_12.dll` / cuDNN at runtime. The `nvidia-cublas-cu12` /
    `nvidia-cudnn-cu12` wheels ship those DLLs under `site-packages/nvidia/*/bin`,
    which Windows does NOT search by default — so we add them explicitly before the
    first faster-whisper import. (torch+cu132 ships cuBLAS *13*, a different major,
    so we can't borrow its copy.) No-op off Windows; the Linux wheels resolve via RPATH.
    """
    global _CUDA_DLL_PATH_DONE
    if _CUDA_DLL_PATH_DONE or os.name != "nt":
        return
    # Register every nvidia-* wheel's bin dir (cublas, cudnn, cuda_runtime, cuda_nvrtc, …).
    # cublas64_12.dll in turn loads cudart64_12.dll, so all the CUDA-12 component dirs must
    # be on the search path — not just cuBLAS. Glob `nvidia/*/bin` off the namespace package.
    try:
        import nvidia

        roots = list(getattr(nvidia, "__path__", []))
    except Exception as e:  # noqa: BLE001 — no nvidia wheels → nothing to register
        log.debug("nvidia CUDA wheels not importable: %s", e)
        roots = []
    extra_path: list[str] = []
    for root in roots:
        for bindir in Path(root).glob("*/bin"):
            if bindir.is_dir():
                extra_path.append(str(bindir))
                try:
                    os.add_dll_directory(str(bindir))
                except OSError as e:
                    log.debug("could not add dll dir %s: %s", bindir, e)
    # add_dll_directory alone doesn't reach ctranslate2's transitive LoadLibrary calls
    # for cublas/cudart on Windows — prepending PATH does (LoadLibrary always searches it).
    if extra_path:
        os.environ["PATH"] = os.pathsep.join([*extra_path, os.environ.get("PATH", "")])
    _CUDA_DLL_PATH_DONE = True


def _device() -> str:
    """'cuda' when a GPU is visible, else 'cpu' — mirrors embeddings.py's check."""
    try:
        import torch

        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:  # noqa: BLE001 — torch absent on a lean box → CPU
        return "cpu"


_WHISPER_LOCK = threading.Lock()


def _get_whisper():
    global _WHISPER
    if _WHISPER is not None:
        return _WHISPER
    # Serialize the load so a prewarm thread and a real job can't double-load the model
    # (two ~3 GB WhisperModels briefly in VRAM). Double-checked: skip the lock once warm.
    with _WHISPER_LOCK:
        if _WHISPER is not None:
            return _WHISPER
        _ensure_cuda_dll_path()
        try:
            from faster_whisper import WhisperModel
        except ImportError as e:
            raise ExtractionUnavailable(f"faster-whisper not installed: {e}") from e
        device = _device()
        # int8_float16 on GPU keeps large-v3 light without accuracy loss; int8 on CPU.
        compute_type = "int8_float16" if device == "cuda" else "int8"
        log.info("loading faster-whisper %s on %s (%s)", WHISPER_MODEL, device, compute_type)
        _WHISPER = WhisperModel(WHISPER_MODEL, device=device, compute_type=compute_type)
    return _WHISPER


def prewarm() -> None:
    """Eagerly load the ASR model so the first real transcription isn't a cold-start.

    `WHISPER_MODEL` (default large-v3, ~3 GB) loads lazily on first use; with a single
    GPU-serialized media worker that first job otherwise blocks for minutes while the
    model reads in. The server calls this off the request path at start (a background
    thread) so the model warms during boot/idle, not on the user's first audio/video
    upload. Soft-fail: a box without the engine (or with media extraction disabled)
    just stays lazy and transcribes nothing until configured.
    """
    if not extraction_enabled():
        return
    try:
        _get_whisper()
    except ExtractionUnavailable as e:
        log.info("ASR prewarm skipped (engine unavailable): %s", e)
    except Exception:  # noqa: BLE001 — prewarm must never crash startup
        log.warning("ASR prewarm failed; will retry lazily on first job", exc_info=True)


def _transcribe(path: Path, media_type: str) -> ExtractResult:
    # A silent video (no audio stream) can't be transcribed — that's NOT a failure.
    # Return an empty transcript cleanly; its visual content is still searchable via
    # per-keyframe CLIP (embeddings.embed_video_frames). A video IS a sequence of images.
    if media_type == "video" and not _has_audio_stream(path):
        return ExtractResult(text="", media_type=media_type, engine="no-audio")
    model = _get_whisper()
    # faster-whisper decodes the media's audio stream via PyAV (handles video containers
    # too), so a video file can be passed directly — no separate ffmpeg extraction step.
    segments, _info = model.transcribe(str(path))
    seg_list = list(segments)  # materialize once: diarization needs per-segment timing
    engine = f"faster-whisper:{WHISPER_MODEL}"

    # OPTIONAL speaker diarization (KB_MCP_DIARIZE, default OFF). A pretrained
    # clustering model (deterministic transduction, not an LLM) labels who-spoke-when
    # and prefixes turns with `[Speaker A]: …`. Soft-fail: if the dep/model is absent
    # or diarization errors, fall back to the plain transcript below — extraction with
    # the flag off (or unavailable) is byte-for-byte unchanged.
    if _diarize_enabled():
        labeled = _diarize(path, seg_list)
        if labeled is not None:
            text, speakers = labeled
            return ExtractResult(
                text=text, media_type=media_type, engine=f"{engine}+diarized",
                speakers=speakers,
            )

    text = " ".join(seg.text.strip() for seg in seg_list).strip()
    return ExtractResult(text=text, media_type=media_type, engine=engine)


# ---------------- optional: ASR speaker diarization (KB_MCP_DIARIZE, default OFF) ----


_DIARIZATION_PIPELINE = None
_DIARIZATION_LOCK = threading.Lock()


def _diarize_enabled() -> bool:
    """True only when KB_MCP_DIARIZE is set. OFF by default — diarization never
    changes existing extraction unless explicitly opted in."""
    return bool(os.environ.get("KB_MCP_DIARIZE"))


def _load_diarization_pipeline():
    """Lazy-import + load the pretrained pyannote speaker-diarization pipeline.

    Soft-import seam (patched in tests): a box without the `[diarization]` extra
    raises ImportError here, which `_run_diarization` catches → plain transcript.
    `KB_MCP_DIARIZE_MODEL` overrides the pretrained checkpoint; `HUGGINGFACE_TOKEN`
    (pyannote gates its weights behind a HF token) is honored if set.
    """
    from pyannote.audio import Pipeline  # soft dep — only imported when enabled

    model = os.environ.get("KB_MCP_DIARIZE_MODEL", "pyannote/speaker-diarization-3.1")
    token = os.environ.get("HUGGINGFACE_TOKEN") or os.environ.get("HF_TOKEN")
    return Pipeline.from_pretrained(model, use_auth_token=token)


def _get_diarization_pipeline():
    """Lazy singleton for the diarization pipeline (one load per process)."""
    global _DIARIZATION_PIPELINE
    if _DIARIZATION_PIPELINE is not None:
        return _DIARIZATION_PIPELINE
    with _DIARIZATION_LOCK:
        if _DIARIZATION_PIPELINE is None:
            _DIARIZATION_PIPELINE = _load_diarization_pipeline()
    return _DIARIZATION_PIPELINE


def _run_diarization(path: Path) -> list[tuple[float, float, str]] | None:
    """Run the pretrained diarization pipeline → `[(start, end, raw_label), …]`.

    Soft-fail: returns None when the dep/model is unavailable or anything errors, so
    `_transcribe` falls back to the plain transcript. Never raises.
    """
    try:
        pipeline = _get_diarization_pipeline()
    except ImportError as e:
        log.debug("diarization dep not installed: %s", e)
        return None
    except Exception:  # noqa: BLE001 — model/token/GPU issues must soft-fail, not crash
        log.warning("diarization pipeline load failed; using plain transcript", exc_info=True)
        return None
    try:
        annotation = pipeline(str(path))
        return [
            (float(turn.start), float(turn.end), str(label))
            for turn, _track, label in annotation.itertracks(yield_label=True)
        ]
    except Exception:  # noqa: BLE001 — a bad/unsupported file must soft-fail to plain ASR
        log.warning("diarization run failed for %s; using plain transcript", path.name, exc_info=True)
        return None


def _diarize(
    path: Path, seg_list: list
) -> tuple[str, list[dict]] | None:
    """Label ASR segments with speakers and render `[Speaker A]: …` turns.

    Maps each whisper segment to the diarization speaker whose turn overlaps it most,
    relabels raw `SPEAKER_00/01/…` to first-appearance `Speaker A/B/…`, and merges
    consecutive same-speaker segments into one turn. Returns
    `(labeled_text, speakers)` where `speakers` is the structured turn list, or None
    on soft-fail (no diarization output / no segments) so the caller uses the plain
    transcript.
    """
    turns = _run_diarization(path)
    if not turns or not seg_list:
        return None

    # First-appearance map of raw pyannote labels → "Speaker A", "Speaker B", …
    label_names: dict[str, str] = {}

    def _name(raw: str) -> str:
        if raw not in label_names:
            label_names[raw] = f"Speaker {chr(ord('A') + len(label_names))}"
        return label_names[raw]

    def _speaker_for(start: float, end: float) -> str | None:
        best_label, best_overlap = None, 0.0
        for t_start, t_end, raw in turns:
            overlap = min(end, t_end) - max(start, t_start)
            if overlap > best_overlap:
                best_overlap, best_label = overlap, raw
        return _name(best_label) if best_label is not None else None

    merged: list[dict] = []
    for seg in seg_list:
        seg_text = seg.text.strip()
        if not seg_text:
            continue
        start = float(getattr(seg, "start", 0.0) or 0.0)
        end = float(getattr(seg, "end", start) or start)
        speaker = _speaker_for(start, end) or "Speaker A"
        if merged and merged[-1]["speaker"] == speaker:
            merged[-1]["text"] += " " + seg_text
            merged[-1]["end"] = end
        else:
            merged.append({"speaker": speaker, "start": start, "end": end, "text": seg_text})

    if not merged:
        return None
    labeled_text = "\n".join(f"[{m['speaker']}]: {m['text']}" for m in merged).strip()
    return labeled_text, merged


def _has_audio_stream(path: Path) -> bool:
    """True if the container has an audio stream. On any probing error, assume yes
    (let Whisper try) rather than wrongly skipping a transcribable file."""
    try:
        import av

        with av.open(str(path)) as container:
            return any(s.type == "audio" for s in container.streams)
    except Exception:  # noqa: BLE001 — can't probe → don't pre-empt Whisper
        return True


_TESSERACT_READY = False


def _ensure_tesseract_cmd() -> None:
    """Point pytesseract at the Tesseract binary when it isn't on PATH.

    The UB-Mannheim Windows installer doesn't add Tesseract to PATH, and the
    service process may not inherit a shell PATH that has it. Honor an explicit
    `KB_MCP_TESSERACT_CMD`, else probe the standard install locations. Idempotent.
    """
    global _TESSERACT_READY
    if _TESSERACT_READY:
        return
    import shutil

    import pytesseract

    explicit = os.environ.get("KB_MCP_TESSERACT_CMD")
    if explicit:
        pytesseract.pytesseract.tesseract_cmd = explicit
    elif not shutil.which("tesseract"):
        for cand in (
            r"C:\Program Files\Tesseract-OCR\tesseract.exe",
            r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        ):
            if Path(cand).is_file():
                pytesseract.pytesseract.tesseract_cmd = cand
                break
    _TESSERACT_READY = True


def _ocr_image(path: Path) -> ExtractResult:
    try:
        import pytesseract
        from PIL import Image
    except ImportError as e:
        raise ExtractionUnavailable(f"pytesseract/Pillow not installed: {e}") from e
    _ensure_tesseract_cmd()
    try:
        with Image.open(path) as img:
            text = pytesseract.image_to_string(img).strip()
    except pytesseract.TesseractNotFoundError as e:
        raise ExtractionUnavailable(f"Tesseract binary not on PATH: {e}") from e
    # OPTIONAL frozen-model caption (KB_MCP_VISION_CAPTION, default OFF) prepended so
    # a photo with no on-image text is still findable. Soft-fails to OCR-only.
    text, engine = _maybe_caption(text, path)
    return ExtractResult(text=text, media_type="image", engine=engine)


# ---------------- optional: vision captioning (KB_MCP_VISION_CAPTION, default OFF) ----
#
# PURE-SUBSTRATE NOTE: a FROZEN image-caption model (BLIP/Florence-2 class) is
# deterministic transduction — the same category as Tesseract OCR, CLIP, and the bge
# embedder: it MEASURES the pixels into text, it does not reason. It is NOT a
# server-side reasoning LLM. But because a caption model *generates* natural-language
# text (unlike OCR, which only reads text that is already in the image), it ships
# DEFAULT-OFF: flip KB_MCP_VISION_CAPTION only once you've confirmed the configured
# model is the frozen captioner you intend. With the flag off (or the dep/model/GPU
# absent) `_ocr_image` returns byte-for-byte the same OCR-only result as before.


_CAPTIONER = None
_CAPTIONER_LOCK = threading.Lock()


def _vision_caption_enabled() -> bool:
    """True only when KB_MCP_VISION_CAPTION is set. OFF by default (see the
    pure-substrate note above) — captioning never changes OCR output unless opted in."""
    return bool(os.environ.get("KB_MCP_VISION_CAPTION"))


def _caption_model_name() -> str:
    """The frozen caption checkpoint; KB_MCP_VISION_CAPTION_MODEL overrides it."""
    return os.environ.get("KB_MCP_VISION_CAPTION_MODEL", "Salesforce/blip-image-captioning-large")


def _load_captioner():
    """Lazy-import + load the frozen caption pipeline (transformers image-to-text).

    Soft-import seam (patched in tests): a box without the `[vision]` extra raises
    ImportError here, which `_caption_image` catches → OCR-only.
    """
    from transformers import pipeline  # soft dep — only imported when enabled

    device = 0 if _device() == "cuda" else -1
    return pipeline("image-to-text", model=_caption_model_name(), device=device)


def _get_captioner():
    """Lazy singleton for the caption pipeline (one load per process)."""
    global _CAPTIONER
    if _CAPTIONER is not None:
        return _CAPTIONER
    with _CAPTIONER_LOCK:
        if _CAPTIONER is None:
            _CAPTIONER = _load_captioner()
    return _CAPTIONER


def _caption_image(path: Path) -> str | None:
    """Frozen caption model → a one-line description, or None on soft-fail.

    Deterministic transduction (a frozen captioner, not an LLM). Never raises: a
    missing dep/model/GPU or any inference error returns None so the caller keeps the
    OCR-only text.
    """
    try:
        captioner = _get_captioner()
    except ImportError as e:
        log.debug("vision-caption dep not installed: %s", e)
        return None
    except Exception:  # noqa: BLE001 — model/GPU load issues must soft-fail, not crash
        log.warning("vision-caption model load failed; using OCR only", exc_info=True)
        return None
    try:
        out = captioner(str(path))
        # transformers image-to-text returns [{"generated_text": "..."}].
        if isinstance(out, list) and out and isinstance(out[0], dict):
            caption = str(out[0].get("generated_text", "")).strip()
            return caption or None
        return None
    except Exception:  # noqa: BLE001 — a bad image must soft-fail to OCR-only
        log.warning("vision-caption inference failed for %s; using OCR only", path.name, exc_info=True)
        return None


def _maybe_caption(ocr_text: str, path: Path) -> tuple[str, str]:
    """Return `(text, engine)` for an OCR'd image, prepending a frozen-model caption
    when KB_MCP_VISION_CAPTION is enabled and captioning succeeds.

    Flag off, or the captioner soft-fails → the unchanged OCR text + `"tesseract"`.
    Caption present → `"<caption>\\n\\n<ocr_text>"` + `"tesseract+<model-short>"`.
    """
    if not _vision_caption_enabled():
        return ocr_text, "tesseract"
    caption = _caption_image(path)
    if not caption:
        return ocr_text, "tesseract"
    text = f"{caption}\n\n{ocr_text}".strip() if ocr_text else caption
    short = _caption_model_name().rsplit("/", 1)[-1]
    return text, f"tesseract+{short}"


def _extract_pdf(path: Path) -> ExtractResult:
    try:
        import fitz  # PyMuPDF
    except ImportError as e:
        raise ExtractionUnavailable(f"pymupdf not installed: {e}") from e
    warnings: list[str] = []
    parts: list[str] = []
    ocr_pages = 0
    with fitz.open(path) as doc:
        for page in doc:
            page_text = page.get_text().strip()
            if len(page_text) < _PDF_OCR_MIN_CHARS:
                # Scanned/image-only page → rasterize and OCR it.
                ocr_text = _ocr_pdf_page(page)
                if ocr_text:
                    page_text = ocr_text
                    ocr_pages += 1
            if page_text:
                parts.append(page_text)
    if ocr_pages:
        warnings.append(f"{ocr_pages} scanned page(s) recovered via OCR")
    engine = "pymupdf+tesseract" if ocr_pages else "pymupdf"
    return ExtractResult(text="\n\n".join(parts).strip(), media_type="pdf", engine=engine, warnings=warnings)


def _extract_document(path: Path, media_type: str) -> ExtractResult:
    """docx/xlsx/pptx/html → markdown via MarkItDown (Microsoft, MIT; office libs bundled).

    Runs fully local — plugins disabled, no cloud/LLM. PDF deliberately does NOT route
    here: markitdown's PDF path is weaker than PyMuPDF + our scanned-page OCR fallback.
    """
    try:
        from markitdown import MarkItDown
    except ImportError as e:
        raise ExtractionUnavailable(f"markitdown not installed: {e}") from e
    try:
        result = MarkItDown(enable_plugins=False).convert(str(path))
    except Exception as e:  # noqa: BLE001 — a malformed doc must not crash the worker
        raise ExtractionUnavailable(f"markitdown could not convert {path.name!r}: {e}") from e
    text = (getattr(result, "text_content", "") or "").strip()
    return ExtractResult(text=text, media_type=media_type, engine="markitdown")


def _extract_textfile(path: Path) -> ExtractResult:
    """Plain-text files: read as UTF-8 (undecodable bytes replaced). No dependency."""
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    return ExtractResult(text=text, media_type="text", engine="text")


def _extract_eml(path: Path) -> ExtractResult:
    """.eml → key headers + the plain/HTML body, via the stdlib email parser (no dep)."""
    import email
    from email import policy

    msg = email.message_from_bytes(path.read_bytes(), policy=policy.default)
    head = [f"{h}: {msg[h]}" for h in ("From", "To", "Cc", "Subject", "Date") if msg[h]]
    body = ""
    try:
        part = msg.get_body(preferencelist=("plain", "html"))
        if part is not None:
            body = part.get_content()
    except Exception:  # noqa: BLE001 — exotic MIME shouldn't fail the whole extract
        body = ""
    text = ("\n".join(head) + "\n\n" + (body or "")).strip()
    return ExtractResult(text=text, media_type="email", engine="email")


_ICS_FIELDS = ("SUMMARY", "DESCRIPTION", "LOCATION", "DTSTART", "DTEND", "ORGANIZER", "ATTENDEE")


def _extract_ics(path: Path) -> ExtractResult:
    """.ics → human-meaningful VEVENT fields. Minimal native parse (RFC 5545 line
    unfolding); no `icalendar` dependency for what is a low-volume format."""
    raw = path.read_text(encoding="utf-8", errors="replace")
    unfolded = re.sub(r"\r?\n[ \t]", "", raw)  # join folded continuation lines
    lines: list[str] = []
    for line in unfolded.splitlines():
        name = line.split(":", 1)[0].split(";", 1)[0].upper()
        if name in _ICS_FIELDS and ":" in line:
            lines.append(f"{name}: {line.split(':', 1)[1]}")
    return ExtractResult(text="\n".join(lines).strip(), media_type="calendar", engine="ics")


def _ocr_pdf_page(page) -> str:
    """Rasterize a PDF page to an image and OCR it. Empty string if OCR is unavailable."""
    try:
        import io

        import pytesseract
        from PIL import Image

        _ensure_tesseract_cmd()
        pix = page.get_pixmap(dpi=200)
        with Image.open(io.BytesIO(pix.tobytes("png"))) as img:
            return pytesseract.image_to_string(img).strip()
    except Exception as e:  # noqa: BLE001 — OCR fallback is best-effort
        log.warning("PDF page OCR fallback failed: %s", e)
        return ""
