"""Offline Whisper transcription: the ONNX ASR adapter's model with Whisper's long-form decoding and seek."""

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
from .asr_facts import AsrFacts
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


def decode_audio(path: Path, sample_rate: int) -> np.ndarray:
    """The whole recording as mono float32, through the same resampler as the chunked reader.

    Samples keep their decoded float values, so loud passages are not clipped to full scale.
    """
    chunks = list(audio_chunks(path, sample_rate, sample_rate * 30))
    return np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)


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
    # A record that cannot be read or parsed counts as absent, so the file starts again.
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) and isinstance(record.get("next_offset"), int) else None


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


def _log_softmax(logits: np.ndarray) -> np.ndarray:
    peak = np.max(logits)
    return logits - (peak + np.log(np.sum(np.exp(logits - peak))))


def _apply_timestamp_rules(
    logits: np.ndarray, generated: list[int], *, end_token: int, timestamp_begin: int, max_initial_timestamp: int,
) -> None:
    """OpenAI's ApplyTimestampRules: timestamps come in pairs, never go back, and win when they outweigh text."""
    stamps = [token for token in generated if token >= timestamp_begin]
    after_stamp = bool(generated) and generated[-1] >= timestamp_begin
    after_pair = len(generated) < 2 or generated[-2] >= timestamp_begin
    if after_stamp:
        if after_pair:
            logits[timestamp_begin:] = -np.inf
        else:
            logits[:end_token] = -np.inf
    if stamps:
        floor = stamps[-1] if after_stamp and not after_pair else stamps[-1] + 1
        logits[timestamp_begin:floor] = -np.inf
    if not generated:
        logits[:timestamp_begin] = -np.inf
        logits[timestamp_begin + max_initial_timestamp + 1:] = -np.inf
    logprobs = _log_softmax(logits)
    if np.logaddexp.reduce(logprobs[timestamp_begin:]) > np.max(logprobs[:timestamp_begin]):
        logits[:timestamp_begin] = -np.inf


def decode_window(
    step: Callable[[list[int], object], tuple[np.ndarray, object]],
    prompt: list[int],
    *,
    policy: dict,
    suppressed: np.ndarray,
    begin_suppressed: np.ndarray,
    end_token: int,
    timestamp_begin: int,
    max_initial_timestamp: int,
    detokenize: Callable[[list[int]], str],
    temperature: float,
    rng: np.random.Generator,
) -> Decoded:
    """Decode one window with timestamp tokens, greedily or by sampling above zero temperature, within the token cap."""
    tokens = list(prompt)
    generated: list[int] = []
    state = None
    logprob_sum = 0.0
    for index in range(policy["max_new_tokens"]):
        logits, state = step(tokens, state)
        logits = np.asarray(logits, dtype=np.float64).copy()
        logits[suppressed] = -np.inf
        # The tokenizer fixes the special tokens between end-of-text and the first timestamp.
        logits[end_token + 1:timestamp_begin] = -np.inf
        if index == 0:
            logits[begin_suppressed] = -np.inf
        _apply_timestamp_rules(
            logits, generated, end_token=end_token, timestamp_begin=timestamp_begin,
            max_initial_timestamp=max_initial_timestamp,
        )
        peak = np.max(logits)
        logprobs = _log_softmax(logits)
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
        spoken = [token for token in generated if token < end_token]
        if spoken and compression_ratio(detokenize(spoken)) > policy["compression_ratio_threshold"]:
            break
    spoken = [token for token in generated if token < end_token]
    text = detokenize(spoken) if spoken else ""
    return Decoded(tuple(generated), text, logprob_sum / (len(generated) + 1), compression_ratio(text))


#: Seconds one timestamp token spans: the Whisper tokenizer's fixed time precision.
TIMESTAMP_SECONDS = 0.02


@dataclass(frozen=True)
class TimedTokens:
    """Text tokens of one segment, with its start and end in samples from the window's start."""

    start: int
    end: int
    tokens: tuple[int, ...]


@dataclass(frozen=True)
class WindowSplit:
    segments: tuple[TimedTokens, ...]
    seek: int


def split_window(tokens: tuple[int, ...] | list[int], *, timestamp_begin: int, window_samples: int, rate: int) -> WindowSplit:
    """Cut a window's tokens into segments and decide how far to seek, as OpenAI's transcribe.py does."""
    step = rate * TIMESTAMP_SECONDS

    def sample(token: int) -> int:
        return min(int(round((token - timestamp_begin) * step)), window_samples)

    def timed(part: list[int]) -> TimedTokens:
        return TimedTokens(sample(part[0]), sample(part[-1]), tuple(t for t in part if t < timestamp_begin))

    is_stamp = [token >= timestamp_begin for token in tokens]
    single_ending = len(tokens) >= 2 and not is_stamp[-2] and is_stamp[-1]
    cuts = [index + 1 for index in range(len(tokens) - 1) if is_stamp[index] and is_stamp[index + 1]]
    if not cuts:
        stamps = [token for token in tokens if token >= timestamp_begin]
        end = sample(stamps[-1]) if stamps and stamps[-1] != timestamp_begin else window_samples
        return WindowSplit((TimedTokens(0, end, tuple(t for t in tokens if t < timestamp_begin)),), window_samples)
    if single_ending:
        cuts.append(len(tokens))
    segments, previous = [], 0
    for cut in cuts:
        segments.append(timed(list(tokens[previous:cut])))
        previous = cut
    content_end = sample(tokens[previous - 1])
    return WindowSplit(tuple(segments), window_samples if single_ending else content_end)


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


class _SeekBuffer:
    """Samples from the current seek on, read from the audio stream as far as one window ahead."""

    def __init__(self, chunks: Iterator[np.ndarray]) -> None:
        self._chunks = chunks
        self._samples = np.empty(0, dtype=np.float32)
        self._start = 0  # absolute sample index of self._samples[0]
        self._ended = False

    def _drop_before(self, seek: int) -> None:
        drop = min(seek - self._start, len(self._samples))
        if drop > 0:
            self._samples = self._samples[drop:]
            self._start += drop

    def window(self, seek: int, length: int) -> np.ndarray:
        """The window of up to `length` samples at `seek`."""
        self._drop_before(seek)
        while len(self._samples) < seek - self._start + length and not self._ended:
            chunk = next(self._chunks, None)
            if chunk is None:
                self._ended = True
                break
            self._samples = np.concatenate([self._samples, chunk])
            self._drop_before(seek)
        begin = seek - self._start
        return self._samples[begin:begin + length]


class OnnxWhisperBackend:
    name = "onnx-asr"
    device = "cpu"  # the session loads the CPU execution provider only

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
        runtime_resources.configure_onnx_media_session_options(options, default_threads=2)
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
        self._prompt_tokens = (int(generation["task_to_id"]["transcribe"]),)
        self._timestamp_begin = int(generation["no_timestamps_token_id"]) + 1
        self._max_initial_timestamp = int(generation["max_initial_timestamp_index"])
        self._languages = {int(token_id): token for token, token_id in generation["lang_to_id"].items()}
        # The model publishes tokens like "<|xx|>"; a caller names a language by its "xx" code.
        self._language_ids = {token.removeprefix("<|").removesuffix("|>"): int(token_id)
                              for token, token_id in generation["lang_to_id"].items()}
        protocol = {self._start_token, int(generation["prev_sot_token_id"]), *map(int, generation["task_to_id"].values())}
        self._suppressed = np.asarray(sorted({*map(int, generation["suppress_tokens"]), *protocol}), dtype=np.int64)
        self._begin_suppressed = np.asarray(generation["begin_suppress_tokens"], dtype=np.int64)
        policy = self.spec["decoding"]
        # The prompt (start, language, task) and the new tokens share the text context.
        if not policy["temperatures"] or policy["max_new_tokens"] + 3 > int(generation["max_length"]):
            raise ValueError("the speech registry's decoding policy does not fit the model")
        self._model, self._vad = model, vad

    def _step(self, encoding) -> Callable[[list[int], object], tuple[np.ndarray, object]]:
        whisper = self._model.asr

        def step(tokens: list[int], state: object) -> tuple[np.ndarray, object]:
            state = whisper._create_state() if state is None else state
            logits, state = whisper._decode(np.asarray([tokens], dtype=np.int64), state, encoding)
            return logits[0, -1], state

        return step

    def _detect_language(self, encoding) -> tuple[int, float]:
        """Choose this recording's language token from the model's own language tokens.

        The probability is a softmax over those tokens alone, so it reads as confidence among languages.
        """
        logits, _ = self._step(encoding)([self._start_token], None)
        candidates = np.fromiter(self._languages, dtype=np.int64)
        scores = logits[candidates].astype(np.float64)
        probabilities = np.exp(scores - np.max(scores))
        probabilities /= probabilities.sum()
        best = int(np.argmax(probabilities))
        return int(candidates[best]), float(probabilities[best])

    def _language_id(self, code: str) -> int:
        """The token for a language code the model publishes; any other code is refused, never replaced."""
        if code not in self._language_ids:
            from .extract import UnsupportedLanguage

            raise UnsupportedLanguage(code, sorted(self._language_ids))
        return self._language_ids[code]

    def _language_code(self, token: int) -> str:
        return self._languages[token].removeprefix("<|").removesuffix("|>")

    def _text(self, tokens: list[int]) -> str:
        return self._model.asr._decode_tokens(np.asarray(tokens, dtype=np.int64)).text

    def _recognize(
        self, waveform: np.ndarray, language: int | None, seed: int
    ) -> tuple[int, tuple[int, ...], float | None]:
        """Decode one window; the probability is set only when this call detected the language."""
        whisper = self._model.asr
        encoding = whisper._encode(waveform[None], np.array([len(waveform)], dtype=np.int64))
        probability = None
        if language is None:
            language, probability = self._detect_language(encoding)
        policy = self.spec["decoding"]
        rng = np.random.default_rng(seed)

        def decode(temperature: float) -> Decoded:
            return decode_window(
                self._step(encoding), [self._start_token, language, *self._prompt_tokens], policy=policy,
                suppressed=self._suppressed, begin_suppressed=self._begin_suppressed, end_token=self._end_token,
                timestamp_begin=self._timestamp_begin, max_initial_timestamp=self._max_initial_timestamp,
                detokenize=self._text, temperature=temperature, rng=rng,
            )

        return language, decode_with_fallback(decode, policy).tokens, probability

    def transcribe(
        self,
        path: Path,
        *,
        vault_root: Path | None = None,
        language: str | None = None,
        multilingual: bool = False,
        report: AsrFacts | None = None,
        policy: dict | None = None,
    ) -> tuple[Iterator[Segment], str]:
        """Transcribe by seeking through the audio; with a vault, a stopped run resumes at its last seek.

        A `language` code replaces detection for the whole file. `report` receives what the engine decided.
        """
        if multilingual:  # one language serves the whole file here, so the mode cannot be honoured
            from .extract import MultilingualUnsupported

            raise MultilingualUnsupported("onnx-asr")
        label = (
            f"onnx-asr:{self.spec['model']}:{self.spec['artifact']['digest'][:16]}"
            f":decoding-{self.spec['decoding']['revision']}"
        )

        def segments() -> Iterator[Segment]:
            with runtime_resources.model_execution():
                self._load()
                requested = self._language_id(language) if language else None
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
                    or record.get("language_requested") != language
                ):
                    record = None
                done = [Segment(**segment) for segment in record["segments"]] if record else []
                seek = int(record["next_offset"]) if record else 0
                # The token chosen for the file: the caller's, else the first spoken window's detection.
                chosen = requested if requested is not None else (record["language"] if record else None)
                probability = record.get("language_probability") if record and requested is None else None
                yield from done
                samples = _SeekBuffer(audio_chunks(path, rate, chunk_samples))
                while True:
                    window = samples.window(seek, chunk_samples)
                    length = len(window)
                    if not length:
                        break
                    # Normalize only VAD amplitude so quiet speech is not discarded;
                    # Whisper retains the waveform used in the quality comparison.
                    peak = float(np.max(np.abs(window)))
                    if not np.isfinite(peak):
                        raise ValueError("decoded audio contains non-finite samples")
                    vad_waveform = window / peak if peak > 0 else window
                    # Silero's 16 kHz input needs one hop and its context samples.
                    padded = np.pad(vad_waveform, (0, max(0, self.spec["vad_min_samples"] - length)))
                    detected = next(self._vad.segment_batch(padded[None], np.array([len(padded)]), rate))
                    advance = length
                    # VAD skips non-speech but leaves spoken windows unchanged.
                    if next(detected, None) is not None:
                        # Like the local engine, one language serves the whole file.
                        chosen, tokens, detected_probability = self._recognize(window, chosen, seed=seek)
                        probability = detected_probability if detected_probability is not None else probability
                        plan = split_window(
                            tokens, timestamp_begin=self._timestamp_begin, window_samples=length, rate=rate,
                        )
                        advance = plan.seek
                        for timed in plan.segments:
                            text = self._text(list(timed.tokens)).strip() if timed.tokens else ""
                            # Clamping timestamps to this window can erase a span beyond its end.
                            # Drop that text, usually padding hallucination; an overshot final word can also be lost.
                            if text and timed.end > timed.start:
                                segment = Segment((seek + timed.start) / rate, (seek + timed.end) / rate, text)
                                done.append(segment)
                                yield segment
                    seek += advance
                    if progress is not None:
                        _write_progress(vault_root, progress, {
                            "format": PROGRESS_FORMAT, "input_sha256": identity, "engine": label,
                            "chunk_samples": chunk_samples, "language_requested": language,
                            "language": chosen, "language_probability": probability, "next_offset": seek,
                            "segments": [segment.__dict__ for segment in done],
                        })
                if report is not None:
                    report.language = self._language_code(chosen) if chosen is not None else None
                    report.language_probability = probability if requested is None else None
                    report.duration_seconds = seek / rate
                    report.policy = f"decoding-{self.spec['decoding']['revision']}"
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
