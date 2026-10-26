"""Offline Whisper transcription through the maintained ONNX ASR adapter."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import media_models, runtime_resources
from .embedding_backend import ModelFilesUnavailable


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    text: str


def audio_chunks(path: Path, sample_rate: int, chunk_samples: int) -> Iterator[np.ndarray]:
    """Decode audio and video with bounded buffers through PyAV's resampler."""
    import av

    fifo = av.AudioFifo()
    resampler = av.AudioResampler(format="fltp", layout="mono", rate=sample_rate)
    with av.open(str(path)) as container:
        if not container.streams.audio:
            return
        for frame in container.decode(audio=0):
            for decoded in resampler.resample(frame):
                decoded.pts = None
                fifo.write(decoded)
                while fifo.samples >= chunk_samples:
                    yield fifo.read(chunk_samples).to_ndarray()[0]
        for decoded in resampler.resample(None):
            decoded.pts = None
            fifo.write(decoded)
        while fifo.samples:
            yield fifo.read(min(fifo.samples, chunk_samples)).to_ndarray()[0]


class OnnxWhisperBackend:
    def __init__(self) -> None:
        self.spec = media_models.model_spec("speech")
        self._model = None
        self._vad = None

    def _load(self) -> None:
        if self._model is not None:
            return
        try:
            import onnx_asr
            import onnxruntime as ort

            artifact = media_models.ensure_media_model("speech")
        except (ImportError, ModelFilesUnavailable) as error:
            from .extract import ExtractionUnavailable

            raise ExtractionUnavailable(f"offline speech engine unavailable: {error}") from error
        options = ort.SessionOptions()
        runtime_resources.configure_onnx_session_options(options, default_threads=2)
        options.enable_cpu_mem_arena = False
        options.add_session_config_entry("session.disable_prepacking", "1")
        options.add_session_config_entry("session.intra_op.allow_spinning", "0")
        options.add_session_config_entry("session.inter_op.allow_spinning", "0")
        runtime = dict(sess_options=options, providers=["CPUExecutionProvider"])
        model = onnx_asr.load_model(
            "whisper", artifact.root, quantization=artifact.spec["artifact"]["quantization"], **runtime,
        )
        vad = onnx_asr.load_vad("silero", artifact.root / "vad", **runtime)
        self._model, self._vad = model, vad

    def transcribe(self, path: Path) -> tuple[Iterator[Segment], str]:
        def segments() -> Iterator[Segment]:
            with runtime_resources.model_execution():
                self._load()
                rate = self.spec["sample_rate"]
                chunk_samples = int(rate * self.spec["chunk_seconds"])
                offset = 0
                for waveform in audio_chunks(path, rate, chunk_samples):
                    length = len(waveform)
                    # Normalize only VAD amplitude so quiet speech is not discarded;
                    # Whisper retains the waveform used in the quality comparison.
                    peak = float(np.max(np.abs(waveform)))
                    if not np.isfinite(peak):
                        raise ValueError("decoded audio contains non-finite samples")
                    vad_waveform = waveform / peak if peak > 0 else waveform
                    # Silero's 16 kHz input needs one hop and its context samples.
                    padded = np.pad(vad_waveform, (0, max(0, self.spec["vad_min_samples"] - length)))
                    detected = next(self._vad.segment_batch(padded[None], np.array([len(padded)]), rate))
                    # The public Whisper adapter does not expose no-speech probability.
                    # VAD skips non-speech but leaves spoken chunks unchanged.
                    if next(detected, None) is not None:
                        text = self._model.recognize(waveform, sample_rate=rate)
                        if text.strip():
                            yield Segment(offset / rate, (offset + length) / rate, text)
                    offset += length

        label = f"onnx-asr:{self.spec['model']}:{self.spec['artifact']['digest'][:16]}"
        return segments(), label

    def prewarm(self) -> None:
        with runtime_resources.model_execution():
            self._load()


_backend: OnnxWhisperBackend | None = None
_lock = threading.Lock()


def get_backend() -> OnnxWhisperBackend:
    global _backend
    with _lock:
        if _backend is None:
            _backend = OnnxWhisperBackend()
        return _backend
