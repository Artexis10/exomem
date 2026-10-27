"""Offline Whisper transcription: the ONNX ASR adapter's model with Whisper's long-form guards."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import zlib
from collections.abc import Callable, Iterator
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


#: The progress record's own format; the engine label decides whether a record still applies.
PROGRESS_FORMAT = 1


def _progress_path(vault_root: Path, binary_rel: str) -> Path:
    # Keyed by the file's vault path, so removing the file can discard its partial transcript.
    from . import state_paths

    name = hashlib.sha256(binary_rel.encode("utf-8")).hexdigest()
    return state_paths.vault_state_dir(vault_root) / "media-progress" / "speech" / f"{name}.json"


def binary_rel(vault_root: Path, path: Path) -> str | None:
    """The vault-relative path that keys a file's speech progress; None outside the vault."""
    try:
        return path.resolve().relative_to(vault_root.resolve()).as_posix()
    except ValueError:
        return None  # outside the vault: nothing could discard a record, so keep none


def _read_progress(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return None


def _write_progress(vault_root: Path, path: Path, record: dict) -> None:
    from . import state_paths

    state_paths.ensure_vault_state_dir(vault_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=".progress.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(record, stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def progress_offset(vault_root: Path, binary_rel: str) -> int:
    """Audio samples of this vault file already transcribed; only grows until the transcript completes."""
    record = _read_progress(_progress_path(vault_root, binary_rel))
    return 0 if record is None else int(record["next_offset"])


def discard_progress(vault_root: Path, binary_rel: str) -> None:
    """Forget the partial transcript of a removed, purged or retired vault file."""
    _progress_path(vault_root, binary_rel).unlink(missing_ok=True)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(8 << 20):
            digest.update(block)
    return digest.hexdigest()


@dataclass(frozen=True)
class Decoded:
    tokens: tuple[int, ...]
    text: str
    avg_logprob: float
    compression_ratio: float


def compression_ratio(text: str) -> float:
    """Whisper's repetition signal: UTF-8 bytes over their zlib-compressed size."""
    data = text.encode("utf-8")
    return len(data) / len(zlib.compress(data)) if data else 0.0


def decode_window(
    step: Callable[[list[int], object], tuple[np.ndarray, object]],
    prompt: list[int],
    *,
    policy: dict,
    suppressed: np.ndarray,
    begin_suppressed: np.ndarray,
    end_token: int,
    detokenize: Callable[[list[int]], str],
    temperature: float,
    rng: np.random.Generator,
) -> Decoded:
    """Decode one window greedily, or by sampling above zero temperature, within the token cap."""
    tokens = list(prompt)
    generated: list[int] = []
    state = None
    logprob_sum = 0.0
    for index in range(policy["max_new_tokens"]):
        logits, state = step(tokens, state)
        logits = np.asarray(logits, dtype=np.float64).copy()
        logits[suppressed] = -np.inf
        if index == 0:
            logits[begin_suppressed] = -np.inf
        peak = np.max(logits)
        logprobs = logits - (peak + np.log(np.sum(np.exp(logits - peak))))
        if temperature == 0:
            token = int(np.argmax(logits))
        else:
            weights = np.exp((logits - peak) / temperature)
            token = int(rng.choice(logits.size, p=weights / weights.sum()))
        logprob_sum += float(logprobs[token])
        if token == end_token:
            break
        generated.append(token)
        tokens.append(token)
        # Stop a repetition loop as soon as it shows; the fallback decides what survives.
        if compression_ratio(detokenize(generated)) > policy["compression_ratio_threshold"]:
            break
    text = detokenize(generated)
    return Decoded(tuple(generated), text, logprob_sum / (len(generated) + 1), compression_ratio(text))


def decode_with_fallback(decode: Callable[[float], Decoded], policy: dict) -> Decoded:
    """Whisper's temperature fallback: accept the first attempt that is neither repetitive nor improbable."""
    threshold = policy["compression_ratio_threshold"]
    attempts = []
    for temperature in policy["temperatures"]:
        result = decode(temperature)
        if result.compression_ratio <= threshold and result.avg_logprob >= policy["logprob_threshold"]:
            return result
        attempts.append(result)
    plausible = [attempt for attempt in attempts if attempt.compression_ratio <= threshold] or attempts
    return max(plausible, key=lambda attempt: attempt.avg_logprob)


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
        whisper = getattr(model, "asr", None)
        # onnx-asr 0.12 decodes Whisper greedily to 448 tokens with no repetition
        # guard, and detects the language on every call by an argmax over the whole
        # vocabulary, which often yields a task token. Its model's private hooks run
        # Whisper's published long-form guards here instead; uv.lock pins the exact
        # release and the image's speech gate runs them on a real recording.
        if not all(hasattr(whisper, hook) for hook in ("_encode", "_decode", "_create_state", "_decode_tokens")):
            from .extract import ExtractionUnavailable

            raise ExtractionUnavailable("the pinned speech adapter no longer exposes its decoder")
        generation = json.loads((artifact.root / "generation_config.json").read_text(encoding="utf-8"))
        self._start_token = int(generation["decoder_start_token_id"])
        self._end_token = int(generation["eos_token_id"])
        self._prompt_tokens = (int(generation["task_to_id"]["transcribe"]), int(generation["no_timestamps_token_id"]))
        self._languages = {int(token_id): token for token, token_id in generation["lang_to_id"].items()}
        protocol = {self._start_token, int(generation["prev_sot_token_id"]), *map(int, generation["task_to_id"].values())}
        self._suppressed = np.asarray(sorted({*map(int, generation["suppress_tokens"]), *protocol}), dtype=np.int64)
        self._begin_suppressed = np.asarray(generation["begin_suppress_tokens"], dtype=np.int64)
        policy = self.spec["decoding"]
        # The prompt (start, language, task, no-timestamps) and the new tokens share the text context.
        if not policy["temperatures"] or policy["max_new_tokens"] + 4 > int(generation["max_length"]):
            raise ValueError("the speech registry's decoding policy does not fit the model")
        self._model, self._vad = model, vad

    def _step(self, encoding) -> Callable[[list[int], object], tuple[np.ndarray, object]]:
        whisper = self._model.asr

        def step(tokens: list[int], state: object) -> tuple[np.ndarray, object]:
            state = whisper._create_state() if state is None else state
            logits, state = whisper._decode(np.asarray([tokens], dtype=np.int64), state, encoding)
            return logits[0, -1], state

        return step

    def _detect_language(self, encoding) -> int:
        """Choose this recording's language token from the model's own language tokens."""
        logits, _ = self._step(encoding)([self._start_token], None)
        candidates = np.fromiter(self._languages, dtype=np.int64)
        return int(candidates[np.argmax(logits[candidates])])

    def _recognize(self, waveform: np.ndarray, language: int | None, seed: int) -> tuple[int, str]:
        whisper = self._model.asr
        encoding = whisper._encode(waveform[None], np.array([len(waveform)], dtype=np.int64))
        language = language or self._detect_language(encoding)
        policy = self.spec["decoding"]
        rng = np.random.default_rng(seed)

        def detokenize(tokens: list[int]) -> str:
            return whisper._decode_tokens(np.asarray(tokens, dtype=np.int64)).text

        def decode(temperature: float) -> Decoded:
            return decode_window(
                self._step(encoding), [self._start_token, language, *self._prompt_tokens], policy=policy,
                suppressed=self._suppressed, begin_suppressed=self._begin_suppressed, end_token=self._end_token,
                detokenize=detokenize, temperature=temperature, rng=rng,
            )

        return language, decode_with_fallback(decode, policy).text

    def transcribe(self, path: Path, *, vault_root: Path | None = None) -> tuple[Iterator[Segment], str]:
        """Transcribe in fixed windows; with a vault, a stopped run resumes from its last window."""
        label = (
            f"onnx-asr:{self.spec['model']}:{self.spec['artifact']['digest'][:16]}"
            f":decoding-{self.spec['decoding']['revision']}"
        )

        def segments() -> Iterator[Segment]:
            with runtime_resources.model_execution():
                self._load()
                rate = self.spec["sample_rate"]
                chunk_samples = int(rate * self.spec["chunk_seconds"])
                rel = binary_rel(vault_root, path) if vault_root is not None else None
                progress = _progress_path(vault_root, rel) if rel is not None else None
                identity = _file_sha256(path) if progress is not None else None
                record = _read_progress(progress) if progress is not None else None
                if record is not None and (
                    record.get("format") != PROGRESS_FORMAT
                    or record.get("input_sha256") != identity
                    or record.get("engine") != label
                    or record.get("chunk_samples") != chunk_samples
                ):
                    record = None
                done = [Segment(**segment) for segment in record["segments"]] if record else []
                resume_at = int(record["next_offset"]) if record else 0
                language = record["language"] if record else None
                yield from done
                offset = 0
                for waveform in audio_chunks(path, rate, chunk_samples):
                    length = len(waveform)
                    if offset + length <= resume_at:
                        offset += length
                        continue
                    # Normalize only VAD amplitude so quiet speech is not discarded;
                    # Whisper retains the waveform used in the quality comparison.
                    peak = float(np.max(np.abs(waveform)))
                    if not np.isfinite(peak):
                        raise ValueError("decoded audio contains non-finite samples")
                    vad_waveform = waveform / peak if peak > 0 else waveform
                    # Silero's 16 kHz input needs one hop and its context samples.
                    padded = np.pad(vad_waveform, (0, max(0, self.spec["vad_min_samples"] - length)))
                    detected = next(self._vad.segment_batch(padded[None], np.array([len(padded)]), rate))
                    # VAD skips non-speech but leaves spoken chunks unchanged.
                    if next(detected, None) is not None:
                        # Like the local engine, one language serves the whole file.
                        language, text = self._recognize(waveform, language, seed=offset // chunk_samples)
                        if text.strip():
                            segment = Segment(offset / rate, (offset + length) / rate, text)
                            done.append(segment)
                            yield segment
                    offset += length
                    if progress is not None:
                        _write_progress(vault_root, progress, {
                            "format": PROGRESS_FORMAT, "input_sha256": identity, "engine": label,
                            "chunk_samples": chunk_samples,
                            "language": language, "next_offset": offset,
                            "segments": [segment.__dict__ for segment in done],
                        })
                if progress is not None:
                    progress.unlink(missing_ok=True)

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
