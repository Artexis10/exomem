"""Server-side media extraction — deterministic modality→text (transduction, not a brain).

ASR (audio/video via faster-whisper), OCR (images via Tesseract), and embedded-text
(PDF via PyMuPDF, with a rasterize+OCR fallback for scanned pages). This is "measure,"
not "reason" — the same category as the bge-base embedder already running on the GPU;
Claude still does all the thinking. The extracted text feeds the Evidence sidecar so an
otherwise-opaque binary becomes findable by its content.

Each engine is **soft-imported** and lazily loaded: a lean box without the `[media]`
extra (or with `EXOMEM_DISABLE_MEDIA_EXTRACTION` set) simply raises `ExtractionUnavailable`,
and the caller skips server extraction — the model-driven `/upload` `text` path still works.

Engines are swappable behind `extract_text(path, media_type=...) -> ExtractResult`. ASR
specifically runs behind a `TranscriptionBackend` seam (see `get_transcriber`): faster-
whisper (CTranslate2, CUDA/CPU) or mlx-whisper (Apple Silicon Metal GPU), selected by
platform, without touching callers.

Two OPTIONAL deepen-the-moat transducers ship here, both DEFAULT-OFF + soft-fail:

- ASR speaker diarization (`EXOMEM_DIARIZE`): a pretrained clustering model
  (pyannote.audio) labels who-spoke-when and prefixes transcript turns with
  `[Speaker A]: …`. Deterministic (a frozen clustering model, not an LLM), so it is
  pure-substrate "measure" — but it stays off by default and falls back to the plain
  transcript when the dep/model isn't present, so existing extraction is unchanged.
- Vision captioning (`EXOMEM_VISION_CAPTION`): a FROZEN image-caption model
  (BLIP/Florence-2 class) prepends a one-line description to an image's OCR text so a
  photo with no text is still findable. A frozen caption model is deterministic
  transduction — the same class as Tesseract/CLIP/bge — NOT a reasoning LLM. But
  because it *generates* text it ships OFF by default; flip the flag to opt in once
  you've confirmed the model is the frozen captioner you intend. Soft-fails to
  OCR-only when the dep/model/GPU is absent.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from . import accel

log = logging.getLogger(__name__)

_SEMANTIC_SEGMENTS_FALSY = {"", "0", "false", "no", "off"}
_TIMED_LINE_RE = re.compile(
    r"^\[(?:(\d+):)?(\d{1,2}):(\d{2})\](?:\s\[([^\]\n]+)\]:)?\s?(.*)$"
)


def _semantic_segments_enabled() -> bool:
    return (
        os.environ.get("EXOMEM_SEMANTIC_SEGMENTS", "").strip().lower()
        not in _SEMANTIC_SEGMENTS_FALSY
    )


def _semantic_segments_module():
    from . import semantic_segments

    return semantic_segments

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

WHISPER_MODEL = os.environ.get("EXOMEM_WHISPER_MODEL", "large-v3")
# A PDF page yielding fewer than this many characters of embedded text is treated as
# scanned → rasterize + OCR fallback.
_PDF_OCR_MIN_CHARS = 16


@dataclass
class ExtractResult:
    text: str
    media_type: str           # audio|video|image|pdf|docx|xlsx|pptx|html|text|email|calendar
    engine: str               # provenance, e.g. "faster-whisper:large-v3", "tesseract", "pymupdf"
    warnings: list[str] = field(default_factory=list)
    # Optional speaker-diarized turns from ASR (EXOMEM_DIARIZE). Each entry is
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
    """False when EXOMEM_DISABLE_MEDIA_EXTRACTION is set (mirrors the embeddings flag)."""
    return not os.environ.get("EXOMEM_DISABLE_MEDIA_EXTRACTION")


def extract_text(
    path: str | Path, *, media_type: str | None = None, vault_root: Path | None = None
) -> ExtractResult:
    """Extract text from a media file. Raises ExtractionUnavailable if no engine fits/installed.

    `vault_root` (optional) tells named-speaker attribution which vault's voice-profile
    store to match against; without it attribution falls back to env resolution
    (`EXOMEM_VAULT_PATH`), which the CLI does not populate from `--vault`.
    """
    p = Path(path)
    mt = media_type or media_type_for(p)
    if mt in ("audio", "video"):
        return _transcribe(p, mt, vault_root=vault_root)
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
    """ASR (faster-whisper / CTranslate2) compute device — mode-aware, CPU by default.

    CUDA only in `performance` mode with a capable GPU (via `accel.cuda_if_performance`),
    so a normal/quiet server never preloads the ~3 GB Whisper model onto the GPU at boot
    — the idle VRAM the accel-routed models (bge/reranker/CLIP) already avoid. CTranslate2
    can't use MPS, so this is strictly cuda-or-cpu. `EXOMEM_ASR_DEVICE` (cpu | cuda | auto)
    is an explicit override.
    """
    from . import accel

    pref = (os.environ.get("EXOMEM_ASR_DEVICE") or "").strip().lower()
    if pref in ("cpu", "cuda"):
        return pref
    if pref in ("auto", "gpu"):
        return "cuda" if accel.gpu_usable() else "cpu"
    return accel.cuda_if_performance()


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
        get_transcriber().prewarm()
    except ExtractionUnavailable as e:
        log.info("ASR prewarm skipped (engine unavailable): %s", e)
    except Exception:  # noqa: BLE001 — prewarm must never crash startup
        log.warning("ASR prewarm failed; will retry lazily on first job", exc_info=True)


# ---------------- ASR backend seam ----------------
#
# Transcription runs behind a swappable backend so the engine can vary by platform
# without touching `_transcribe`. Two backends ship:
#   - FasterWhisperBackend (CTranslate2): CUDA or CPU. No Metal path.
#   - MlxWhisperBackend (mlx-whisper): Apple Silicon Metal GPU, from the optional
#     `[media-mlx]` extra.
# `get_transcriber()` auto-selects MLX on Apple Silicon when it's installed, else
# faster-whisper; `EXOMEM_ASR_BACKEND=mlx|faster-whisper` forces the choice.

# Default MLX model repo (HF). large-v3 for quality parity with faster-whisper's default;
# override to e.g. `mlx-community/whisper-large-v3-turbo` for a lighter, faster run on a Mac.
MLX_WHISPER_MODEL = os.environ.get(
    "EXOMEM_MLX_WHISPER_MODEL", "mlx-community/whisper-large-v3-mlx"
)


class TranscriptionSegment(Protocol):
    """The per-segment fields `_transcribe` consumes (text + timing)."""

    text: str
    start: float
    end: float


class TranscriptionBackend(Protocol):
    def transcribe(self, path: Path) -> tuple[Iterable[TranscriptionSegment], str]:
        """Transcribe `path` → ``(segments, engine_label)``.

        ``segments`` yields objects exposing ``.text`` / ``.start`` / ``.end``;
        ``engine_label`` is the provenance string stored on the sidecar (e.g.
        ``faster-whisper:large-v3``).
        """
        ...

    def prewarm(self) -> None:
        """Eagerly load the model so the first real job isn't a cold start."""
        ...


class FasterWhisperBackend:
    """CTranslate2 faster-whisper — CUDA or CPU (never Metal). Device and compute_type
    live in `_get_whisper`; this wrapper keeps the loader seam that tests patch."""

    def transcribe(self, path: Path) -> tuple[Iterable[TranscriptionSegment], str]:
        model = _get_whisper()
        # faster-whisper decodes the media's audio stream via PyAV (handles video
        # containers too), so a video file can be passed directly — no ffmpeg step.
        segments, _info = model.transcribe(str(path))
        return segments, f"faster-whisper:{WHISPER_MODEL}"

    def prewarm(self) -> None:
        _get_whisper()


class _MlxSegment:
    """Adapts an mlx-whisper segment dict to the ``.text``/``.start``/``.end`` attribute
    shape the extractor consumes (faster-whisper yields objects; MLX yields dicts)."""

    __slots__ = ("text", "start", "end")

    def __init__(self, seg: dict):
        self.text = seg.get("text", "")
        self.start = float(seg.get("start", 0.0) or 0.0)
        self.end = float(seg.get("end", self.start) or self.start)


class MlxWhisperBackend:
    """mlx-whisper on Apple Silicon (Metal GPU) — what accelerates ASR on a Mac, since
    faster-whisper/CTranslate2 has no Metal path. Audio is decoded via PyAV (the shared
    16 kHz whisper timebase) when faster-whisper is present, else the file path is handed
    to mlx-whisper's own loader. MLX caches the model in memory after the first load."""

    @staticmethod
    def _require_mlx():
        try:
            import mlx_whisper
        except ImportError as e:  # the optional [media-mlx] extra isn't installed
            raise ExtractionUnavailable(f"mlx-whisper not installed: {e}") from e
        return mlx_whisper

    @staticmethod
    def _load_audio(path: Path):
        """16 kHz mono float32 array via PyAV when available (no ffmpeg, shared timebase);
        else the path string, which mlx-whisper decodes itself (via ffmpeg)."""
        try:
            from faster_whisper.audio import decode_audio
        except ImportError:
            return str(path)
        return decode_audio(str(path), sampling_rate=16000)

    def transcribe(self, path: Path) -> tuple[Iterable[TranscriptionSegment], str]:
        mlx_whisper = self._require_mlx()
        result = mlx_whisper.transcribe(
            self._load_audio(path), path_or_hf_repo=MLX_WHISPER_MODEL
        )
        segments = [_MlxSegment(s) for s in result.get("segments", [])]
        return segments, f"mlx-whisper:{MLX_WHISPER_MODEL}"

    def prewarm(self) -> None:
        import numpy as np

        mlx_whisper = self._require_mlx()
        # A 1 s silent buffer forces the download + model load into MLX's in-memory cache
        # (public API only — no reliance on mlx-whisper internals), so the first real job
        # isn't a cold start.
        mlx_whisper.transcribe(
            np.zeros(16000, dtype=np.float32), path_or_hf_repo=MLX_WHISPER_MODEL
        )


def _mlx_available() -> bool:
    """True on Apple Silicon with mlx-whisper importable (the [media-mlx] extra)."""
    import platform
    import sys

    if sys.platform != "darwin" or platform.machine() != "arm64":
        return False
    import importlib.util

    return importlib.util.find_spec("mlx_whisper") is not None


def get_transcriber() -> TranscriptionBackend:
    """The active ASR backend: mlx-whisper on Apple Silicon (Metal GPU) when available,
    else faster-whisper (CUDA/CPU). Force the choice with
    ``EXOMEM_ASR_BACKEND=mlx|faster-whisper``."""
    pref = (os.environ.get("EXOMEM_ASR_BACKEND") or "auto").strip().lower()
    if pref == "mlx":
        return MlxWhisperBackend()
    if pref in ("faster-whisper", "faster_whisper", "ctranslate2"):
        return FasterWhisperBackend()
    if _mlx_available():  # auto
        return MlxWhisperBackend()
    return FasterWhisperBackend()


def log_diarization_readiness(vault_root: Path | None = None) -> None:
    """One boot-time log line saying whether diarization can actually run.

    Diarization soft-fails by design (a broken stack degrades to plain transcripts), so
    without this line a missing sidecar venv / HF token / profile store is invisible in
    the output. WARNING when the flag is on but a prerequisite is missing, else INFO.
    Token presence is reported as a boolean — the value never enters the log. Never
    raises (same contract as `prewarm`).
    """
    try:
        from . import voice_profiles

        enabled = _diarize_enabled()
        sidecar_venv = _diarizer_sidecar_python() is not None
        hf_token = bool(os.environ.get("HUGGINGFACE_TOKEN") or os.environ.get("HF_TOKEN"))
        names: list[str] = []
        if vault_root is not None:
            names = sorted(
                voice_profiles.load_profiles(voice_profiles.voice_profiles_path(vault_root))
            )
        line = (
            f"diarization readiness: enabled={enabled} sidecar_venv={sidecar_venv} "
            f"hf_token={hf_token} profiles={len(names)}"
        )
        if names:
            line += f" ({', '.join(names)})"
        level = logging.WARNING if enabled and not (sidecar_venv and hf_token) else logging.INFO
        log.log(level, "%s", line)
    except Exception:  # noqa: BLE001 — a diagnostics line must never break startup
        log.debug("diarization readiness check failed", exc_info=True)


def _transcribe(path: Path, media_type: str, vault_root: Path | None = None) -> ExtractResult:
    # A silent video (no audio stream) can't be transcribed — that's NOT a failure.
    # Return an empty transcript cleanly; its visual content is still searchable via
    # per-keyframe CLIP (embeddings.embed_video_frames). A video IS a sequence of images.
    if media_type == "video" and not _has_audio_stream(path):
        return ExtractResult(text="", media_type=media_type, engine="no-audio")
    segments, engine = get_transcriber().transcribe(path)
    seg_list = list(segments)  # materialize once: diarization needs per-segment timing

    # OPTIONAL speaker diarization (EXOMEM_DIARIZE, default OFF). A pretrained
    # clustering model (deterministic transduction, not an LLM) labels who-spoke-when
    # and prefixes turns with `[Speaker A]: …`. Soft-fail: if the dep/model is absent
    # or diarization errors, fall back to the plain transcript below — extraction with
    # the flag off (or unavailable) is byte-for-byte unchanged.
    if _diarize_enabled():
        # Boundary guard for the WHOLE optional layer: the spec's Soft-Fail Degradation
        # requirement says a diarization failure MUST NOT break extraction, and inner
        # guards can't cover everything (a mid-run source change once escaped via an
        # unguarded import). Any exception here degrades to the plain transcript.
        try:
            labeled = _diarize(path, seg_list, vault_root=vault_root)
        except Exception:  # noqa: BLE001 — diarization must never break extraction
            log.warning(
                "diarization failed for %s; using plain transcript", path.name, exc_info=True
            )
            labeled = None
        if labeled is not None:
            text, speakers = labeled
            # `+timed` is detected from the rendered text (not re-checked from the
            # gate) so a soft-failed timed render can never mislabel the engine —
            # the marker is backfill's idempotency key. Order matters:
            # `_needs_rediarize` matches endswith("+diarized").
            timed = "+timed" if _is_timed_text(text) else ""
            return ExtractResult(
                text=text, media_type=media_type, engine=f"{engine}{timed}+diarized",
                speakers=speakers,
            )

    # Timed rendering (EXOMEM_SEMANTIC_SEGMENTS, default OFF): one line per ASR
    # segment with a `[m:ss]` prefix — the substrate for semantic segmentation
    # and `transcript_match_at`. Soft-fail: any renderer error falls back to the
    # flat join below; gate unset is byte-identical to it.
    if _semantic_segments_enabled():
        try:
            semantic_segments = _semantic_segments_module()
            timed_text = semantic_segments.render_timed_lines(
                [
                    (
                        float(getattr(seg, "start", 0.0) or 0.0),
                        float(getattr(seg, "end", 0.0) or 0.0),
                        seg.text,
                        None,
                    )
                    for seg in seg_list
                ]
            ).strip()
            if timed_text:
                return ExtractResult(
                    text=timed_text, media_type=media_type, engine=f"{engine}+timed"
                )
        except Exception:  # noqa: BLE001 — timed rendering must never block extraction
            log.warning("timed transcript rendering failed for %s; using flat text", path.name)

    text = " ".join(seg.text.strip() for seg in seg_list).strip()
    return ExtractResult(text=text, media_type=media_type, engine=engine)


def _is_timed_text(text: str) -> bool:
    """True when the transcript's first line carries a `[m:ss]` marker."""
    first = text.split("\n", 1)[0] if text else ""
    m = _TIMED_LINE_RE.match(first)
    return bool(m and m.group(2) is not None)


# ---------------- optional: ASR speaker diarization (EXOMEM_DIARIZE, default OFF) ----
#
# pyannote's who-spoke-when pipeline runs in an ISOLATED CPU-torch sidecar venv (sidecar/diarizer/)
# as a subprocess, NOT in this process: the main service runs a custom torch-2.12+cu132 (Blackwell)
# build for whisper/CLIP/bge, which is fundamentally incompatible with the pyannote/torchaudio
# ecosystem (torchcodec native-lib load failure, torchaudio AudioMetaData / list_audio_backends
# removed in 2.11, speechbrain 1.x LazyModule, hf_hub use_auth_token removal). Those are VERSION
# walls, not GPU walls, so CPU torch alone doesn't fix them — the sidecar pins the canonical
# pyannote-3.1 stack (torch 2.2.2 / torchaudio 2.2.2 / speechbrain 0.5.16 / huggingface_hub 0.25)
# where pyannote is rock-solid. The sidecar returns anonymous turns as JSON; the named-attribution
# layer below (ECAPA voice profiles → cosine) runs HERE on the main venv, unchanged.


_FALSY_ENV = {"", "0", "false", "no", "off"}


def _env_flag(name: str) -> bool:
    """Truthy opt-in parse: unset, '', '0', 'false', 'no', 'off' (any case) → False.

    Opt-in flags gate behavior that must stay OFF unless deliberately enabled; a bare
    presence check would read `EXOMEM_DIARIZE=0` as opted IN.
    """
    return os.environ.get(name, "").strip().lower() not in _FALSY_ENV


def _diarize_enabled() -> bool:
    """True only when EXOMEM_DIARIZE is set truthy. OFF by default — diarization never
    changes existing extraction unless explicitly opted in."""
    return _env_flag("EXOMEM_DIARIZE")


def _diarizer_sidecar_python() -> Path | None:
    """Locate the isolated diarization sidecar's interpreter, or None if unprovisioned.

    `EXOMEM_DIARIZE_SIDECAR_PYTHON` overrides; else the conventional
    `sidecar/diarizer/.venv/{Scripts/python.exe|bin/python}` under the repo root. Returns None
    (→ plain transcript) when the venv isn't built — never auto-builds at runtime.
    """
    override = os.environ.get("EXOMEM_DIARIZE_SIDECAR_PYTHON")
    if override:
        p = Path(override)
        return p if p.is_file() else None
    root = Path(__file__).resolve().parents[2]  # src/exomem/extract.py → repo root
    rel = "Scripts/python.exe" if os.name == "nt" else "bin/python"
    py = root / "sidecar" / "diarizer" / ".venv" / rel
    return py if py.is_file() else None


def _diarizer_worker_script() -> Path:
    return Path(__file__).resolve().parents[2] / "sidecar" / "diarizer" / "worker.py"


def _diarizer_timeout(path: Path) -> float:
    """Subprocess wall-clock budget, scaled by audio duration.

    CPU pyannote runs slower than real time and the FIRST call also downloads weights; a hung
    child blocks the single-threaded media worker, so we over-budget rather than kill a valid long
    job. `EXOMEM_DIARIZE_TIMEOUT` (seconds) overrides. Floor covers the weight download + short
    clips; the duration scale handles long recordings.
    """
    override = os.environ.get("EXOMEM_DIARIZE_TIMEOUT")
    if override:
        try:
            return float(override)
        except ValueError:
            pass
    duration = 0.0
    try:
        import av

        with av.open(str(path)) as container:
            if container.duration:  # AV_TIME_BASE microseconds
                duration = float(container.duration) / 1_000_000.0
    except Exception:  # noqa: BLE001 — can't probe → use the floor
        duration = 0.0
    return max(900.0, duration * 6.0)


def _is_nvidia_wheel_bin(p: str) -> bool:
    """True for a `…/nvidia/<pkg>/bin` dir — the cu12 wheel bins `_ensure_cuda_dll_path` prepends."""
    q = p.replace("\\", "/").lower().rstrip("/")
    return "/nvidia/" in q and q.endswith("/bin")


def _diarizer_child_env() -> dict[str, str]:
    """Env for the sidecar subprocess.

    Merge the parent env (a Windows child needs SystemRoot/PATH) and quiet HF. Device policy from
    `EXOMEM_DIARIZE_DEVICE` (cpu | cuda | auto, default auto): `cpu` forces CUDA off; otherwise the
    GPU stays visible but the main venv's cu12 nvidia wheel bin dirs are stripped from PATH so they
    can't shadow the sidecar's bundled cu130 CUDA/cuDNN (the CLIP/cuDNN shadow class of bug).
    """
    env = {**os.environ, "HF_HUB_DISABLE_PROGRESS_BARS": "1"}
    pref = os.environ.get("EXOMEM_DIARIZE_DEVICE")
    if pref is None or not pref.strip():
        # No explicit override → follow the compute mode: CPU unless performance, so a
        # normal/quiet box doesn't spin the diarizer up on the GPU mid-game. (The sidecar
        # is transient, so this is about not interrupting, not idle VRAM.)
        from . import accel

        pref = accel.cuda_if_performance()  # "cuda" | "cpu"
    else:
        pref = pref.strip().lower()
    if pref == "cpu":
        env["CUDA_VISIBLE_DEVICES"] = ""
        return env
    cleaned = [p for p in env.get("PATH", "").split(os.pathsep) if not _is_nvidia_wheel_bin(p)]
    env["PATH"] = os.pathsep.join(cleaned)
    return env


def _run_diarization(path: Path) -> list[tuple[float, float, str]] | None:
    """Run diarization in the isolated CPU-torch sidecar subprocess → `[(start, end, raw_label), …]`.

    Soft-fail: returns None on ANY failure (sidecar venv absent, spawn error, nonzero exit, timeout,
    or unparseable output) so `_transcribe` falls back to the plain transcript. Never raises. The
    subprocess inherits this process's env — so `HUGGINGFACE_TOKEN` / `EXOMEM_DIARIZE_MODEL` flow
    through — with CUDA forced off and HF progress bars silenced. The result crosses the boundary as
    a JSON out-file (not stdout, which pyannote/lightning/tqdm can pollute).
    """
    py = _diarizer_sidecar_python()
    if py is None:
        log.warning(
            "EXOMEM_DIARIZE is set but the diarizer sidecar venv is not provisioned "
            "(scripts/setup-diarizer.ps1); using plain transcript"
        )
        return None
    out_fd, out_name = tempfile.mkstemp(prefix="kb_diar_", suffix=".json")
    os.close(out_fd)
    out_path = Path(out_name)
    try:
        try:
            proc = subprocess.run(
                [str(py), str(_diarizer_worker_script()), str(path), str(out_path)],
                capture_output=True,
                text=True,
                timeout=_diarizer_timeout(path),
                env=_diarizer_child_env(),
            )
        except subprocess.TimeoutExpired:
            log.warning("diarizer sidecar timed out for %s; using plain transcript", path.name)
            return None
        except Exception:  # noqa: BLE001 — spawn failure must soft-fail, not crash extraction
            log.warning(
                "diarizer sidecar spawn failed for %s; using plain transcript",
                path.name,
                exc_info=True,
            )
            return None
        if proc.returncode != 0:
            tail = ((proc.stderr or "").strip().splitlines() or [""])[-1]
            log.warning("diarizer sidecar exited %s for %s: %s", proc.returncode, path.name, tail)
            return None
        try:
            turns = json.loads(out_path.read_text(encoding="utf-8"))["turns"]
            return [(float(t["start"]), float(t["end"]), str(t["label"])) for t in turns]
        except Exception:  # noqa: BLE001 — missing/garbled output must soft-fail to plain ASR
            log.warning(
                "diarizer sidecar produced no parseable turns for %s; using plain transcript",
                path.name,
            )
            return None
    finally:
        try:
            out_path.unlink()
        except OSError:
            pass


def _resolve_named_labels(
    path: Path, turns: list[tuple[float, float, str]], vault_root: Path | None = None
) -> dict[str, str] | None:
    """Resolve raw diarization labels → enrolled speaker names, or None to stay anonymous.

    Optional named-attribution layer over the anonymous turns (default-OFF, soft-fail). When
    ≥1 voice profile is enrolled AND voice embedding is available, each raw cluster's spans are
    ECAPA-embedded into a centroid and matched against the profiles by cosine
    (`speaker_attribution.attribute_clusters`). Returns `{raw_label: display_label}` where a
    matched cluster gets the profile name and the rest get stable `Speaker A/B…` (by first
    onset). Returns None — falling through to today's anonymous output — when there are no
    profiles, the embedder is unavailable, or anything fails. Never raises.
    """
    try:
        from . import vault, voice_embed, voice_profiles
        from .speaker_attribution import attribute_clusters

        # Prefer the caller's vault (worker/backfill know it); env resolution is the
        # fallback for callers that don't — a CLI run with only --vault would otherwise
        # silently degrade to anonymous when EXOMEM_VAULT_PATH isn't exported.
        store_path = voice_profiles.voice_profiles_path(
            vault_root if vault_root is not None else vault.resolve_vault()
        )
        profiles = voice_profiles.load_profiles(store_path)
        if not profiles:
            return None  # nobody enrolled → anonymous, no embedding model loaded

        spans_by_label: dict[str, list[tuple[float, float]]] = {}
        first_onset: dict[str, float] = {}
        for t_start, t_end, raw in turns:
            spans_by_label.setdefault(raw, []).append((t_start, t_end))
            first_onset[raw] = min(first_onset.get(raw, float("inf")), t_start)

        # NOTE: embeds per cluster (each call decodes the file once). A load-once
        # embed_clusters() is a tracked follow-up — acceptable here since diarization is
        # opt-in and runs off the request path in the async media worker.
        centroids: dict[str, object] = {}
        for raw, spans in spans_by_label.items():
            vec = voice_embed.embed_spans(path, spans)
            if vec is None:
                return None  # embed soft-fail → wholly anonymous (never partially name)
            centroids[raw] = vec

        attributions = attribute_clusters(centroids, first_onset, profiles)
        return {raw: attr.label for raw, attr in attributions.items()}
    except Exception:  # noqa: BLE001 — attribution must never break extraction
        log.warning("named speaker attribution failed; using anonymous diarization", exc_info=True)
        return None


def _diarize(
    path: Path, seg_list: list, vault_root: Path | None = None
) -> tuple[str, list[dict]] | None:
    """Label ASR segments with speakers and render `[Speaker A]: …` (or `[<name>]: …`) turns.

    Maps each whisper segment to the diarization speaker whose turn overlaps it most, relabels
    raw `SPEAKER_00/01/…` to first-appearance `Speaker A/B/…`, and merges consecutive
    same-speaker segments into one turn. When voice profiles are enrolled and embedding succeeds
    (`_resolve_named_labels`), matched clusters render with the enrolled name instead; everything
    else (no profiles / soft-fail) is byte-identical to the anonymous output. Returns
    `(labeled_text, speakers)` where `speakers` is the structured turn list, or None on soft-fail
    (no diarization output / no segments) so the caller uses the plain transcript.
    """
    turns = _run_diarization(path)
    if not turns or not seg_list:
        return None

    # Optional named layer: raw cluster label → enrolled name (or None → stay anonymous).
    resolved = _resolve_named_labels(path, turns, vault_root=vault_root)

    # First-appearance map of raw pyannote labels → "Speaker A", "Speaker B", …
    label_names: dict[str, str] = {}

    def _name(raw: str) -> str:
        if resolved is not None and raw in resolved:
            return resolved[raw]
        if raw not in label_names:
            label_names[raw] = f"Speaker {chr(ord('A') + len(label_names))}"
        return label_names[raw]

    # Max-overlap segment→turn assignment (earliest-start tiebreak) via the shared, unit-tested
    # helper — keeps assignment logic in one place for both the anonymous and named paths.
    from .speaker_assignment import Turn, assign_span

    turn_objs = [Turn(t_start, t_end, raw) for t_start, t_end, raw in turns]

    def _speaker_for(start: float, end: float) -> str | None:
        raw = assign_span(start, end, turn_objs)
        return _name(raw) if raw is not None else None

    merged: list[dict] = []
    timed_segs: list[tuple[float, float, str, str | None]] = []
    for seg in seg_list:
        seg_text = seg.text.strip()
        if not seg_text:
            continue
        start = float(getattr(seg, "start", 0.0) or 0.0)
        end = float(getattr(seg, "end", start) or start)
        speaker = _speaker_for(start, end) or "Speaker A"
        timed_segs.append((start, end, seg_text, speaker))
        if merged and merged[-1]["speaker"] == speaker:
            merged[-1]["text"] += " " + seg_text
            merged[-1]["end"] = end
        else:
            merged.append({"speaker": speaker, "start": start, "end": end, "text": seg_text})

    if not merged:
        return None
    # Timed rendering (EXOMEM_SEMANTIC_SEGMENTS): one line per ASR segment with the
    # label repeated — merged multi-minute turns would destroy segmentation windows
    # and match localization. The structured `merged` list keeps the merged-turn
    # shape either way (speakers: frontmatter + filters are unaffected). Soft-fail
    # to the merged-turn rendering below.
    if _semantic_segments_enabled():
        try:
            semantic_segments = _semantic_segments_module()
            timed_text = semantic_segments.render_timed_lines(timed_segs).strip()
            if timed_text:
                return timed_text, merged
        except Exception:  # noqa: BLE001 — timed rendering must never block diarization
            log.warning("timed diarized rendering failed for %s; using merged turns", path.name)
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
    `EXOMEM_TESSERACT_CMD`, else probe the standard install locations. Idempotent.
    """
    global _TESSERACT_READY
    if _TESSERACT_READY:
        return
    import shutil

    import pytesseract

    explicit = os.environ.get("EXOMEM_TESSERACT_CMD")
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
    # OPTIONAL frozen-model caption (EXOMEM_VISION_CAPTION, default OFF) prepended so
    # a photo with no on-image text is still findable. Soft-fails to OCR-only.
    text, engine = _maybe_caption(text, path)
    # OPTIONAL CLIP zero-shot tags (EXOMEM_IMAGE_TAGS, default OFF) appended so the image
    # is findable by what it depicts ("invoice", "whiteboard"). Soft-fails to no tags.
    text, engine = _maybe_image_tags(text, path, engine)
    return ExtractResult(text=text, media_type="image", engine=engine)


# ---------------- optional: vision captioning (EXOMEM_VISION_CAPTION, default OFF) ----
#
# PURE-SUBSTRATE NOTE: a FROZEN image-caption model (BLIP/Florence-2 class) is
# deterministic transduction — the same category as Tesseract OCR, CLIP, and the bge
# embedder: it MEASURES the pixels into text, it does not reason. It is NOT a
# server-side reasoning LLM. But because a caption model *generates* natural-language
# text (unlike OCR, which only reads text that is already in the image), it ships
# DEFAULT-OFF: flip EXOMEM_VISION_CAPTION only once you've confirmed the configured
# model is the frozen captioner you intend. With the flag off (or the dep/model/GPU
# absent) `_ocr_image` returns byte-for-byte the same OCR-only result as before.


_CAPTIONER = None
_CAPTIONER_LOCK = threading.Lock()


def _vision_caption_enabled() -> bool:
    """True only when EXOMEM_VISION_CAPTION is set. OFF by default (see the
    pure-substrate note above) — captioning never changes OCR output unless opted in."""
    return _env_flag("EXOMEM_VISION_CAPTION")


def _caption_model_name() -> str:
    """The frozen caption checkpoint; EXOMEM_VISION_CAPTION_MODEL overrides it."""
    return os.environ.get("EXOMEM_VISION_CAPTION_MODEL", "Salesforce/blip-image-captioning-large")


def _load_captioner():
    """Lazy-import + load the frozen caption pipeline (transformers image-to-text).

    Soft-import seam (patched in tests): a box without the `[vision]` extra raises
    ImportError here, which `_caption_image` catches → OCR-only.
    """
    from transformers import pipeline  # soft dep — only imported when enabled

    # This captioner IS a torch model, so it can use MPS on Apple Silicon (unlike
    # the CTranslate2 ASR engine); device via the shared torch-device selector.
    device = accel.pipeline_device()
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
    when EXOMEM_VISION_CAPTION is enabled and captioning succeeds.

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


# ---------------- optional: CLIP zero-shot image tags (EXOMEM_IMAGE_TAGS, default OFF) ----
#
# PURE-SUBSTRATE NOTE: scoring an image's CLIP embedding against a fixed text vocabulary is
# deterministic MEASUREMENT — the same category as Tesseract OCR, CLIP visual search, and the
# bge embedder. It reads pixels into a fixed cosine score against frozen vectors; it does NOT
# generate language and is not a reasoning LLM (cross-image inference stays Claude's). The
# computation + vocabulary live in `image_tags`; this seam just gates it (default-OFF) and
# appends the tags to the indexed text. With the flag off the OCR text flows through unchanged.


def _maybe_image_tags(ocr_text: str, path: Path, engine: str) -> tuple[str, str]:
    """Append CLIP zero-shot tags (EXOMEM_IMAGE_TAGS, default OFF) to an image's extracted text.

    Flag off, no tag clears the threshold, or CLIP soft-fails → unchanged `(ocr_text, engine)`.
    Tags present → `<text>\\n\\nTags: a, b, c` with `+tags` appended to the engine for provenance.
    """
    # Check the gate BEFORE importing image_tags (which pulls in embeddings/CLIP), so the
    # default-off path imports nothing and the output is byte-identical — mirrors _maybe_caption.
    if not os.environ.get("EXOMEM_IMAGE_TAGS"):
        return ocr_text, engine
    from . import image_tags  # lazy: defers the CLIP/embeddings import until opted in

    tags = image_tags.compute_tags(path)
    line = image_tags.format_tags_line(tags)
    if not line:
        return ocr_text, engine
    text = f"{ocr_text}\n\n{line}" if ocr_text else line
    return text, f"{engine}+tags"


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
