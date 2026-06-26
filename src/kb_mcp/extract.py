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
"""

from __future__ import annotations

import logging
import os
import re
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


def _get_whisper():
    global _WHISPER
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
    text = " ".join(seg.text.strip() for seg in segments).strip()
    return ExtractResult(text=text, media_type=media_type, engine=f"faster-whisper:{WHISPER_MODEL}")


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
    return ExtractResult(text=text, media_type="image", engine="tesseract")


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
