"""Audio crosses the model window without losing its decoded tail."""

import json
import wave

import numpy as np
import pytest

from exomem.onnx_asr_runtime import audio_chunks


def test_streamed_audio_keeps_the_tail_after_the_thirty_second_window(tmp_path):
    pytest.importorskip("av")
    path = tmp_path / "stereo.wav"
    source_rate = 48000
    sample_count = source_rate * 31 + source_rate // 8
    samples = np.zeros((sample_count, 2), dtype=np.int16)
    samples[-source_rate // 8:] = 16384
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(source_rate)
        handle.writeframes(samples.tobytes())

    chunks = list(audio_chunks(path, sample_rate=16000, chunk_samples=30 * 16000))

    assert [len(chunk) for chunk in chunks] == [480000, 18000]
    assert all(chunk.dtype == np.float32 for chunk in chunks)
    assert np.max(chunks[-1]) > 0.25


VOCAB = 8
END = 7
POLICY = {
    "max_new_tokens": 40,
    "temperatures": [0.0, 0.2],
    "compression_ratio_threshold": 2.4,
    "logprob_threshold": -1.0,
}


def _scripted(choices):
    """A decoder that strongly prefers the token at each step's position in `choices`."""

    def step(tokens, state):
        position = len(tokens) - 1
        logits = np.full(VOCAB, -20.0)
        logits[choices[min(position, len(choices) - 1)]] = 10.0
        return logits, state

    return step


def _decode(step, temperature=0.0, **overrides):
    from exomem.onnx_asr_runtime import decode_window

    options = dict(
        policy=POLICY, suppressed=np.array([], dtype=np.int64), begin_suppressed=np.array([], dtype=np.int64),
        end_token=END, detokenize=lambda tokens: " ".join(f"w{token}" for token in tokens),
        temperature=temperature, rng=np.random.default_rng(0),
    )
    options.update(overrides)
    return decode_window(step, [0], **options)


def test_a_repetition_loop_stops_before_the_token_cap():
    looping = _decode(_scripted([0, 1, 2, *[3] * 60]))

    assert looping.compression_ratio > POLICY["compression_ratio_threshold"]
    assert len(looping.tokens) < POLICY["max_new_tokens"]


def test_fallback_keeps_the_first_attempt_that_is_neither_repetitive_nor_improbable():
    from exomem.onnx_asr_runtime import decode_with_fallback

    attempts = {0.0: _decode(_scripted([0, 1, 2, *[3] * 60])), 0.2: _decode(_scripted([0, 1, 2, 4, 5, END]))}

    chosen = decode_with_fallback(lambda temperature: attempts[temperature], POLICY)

    assert chosen.tokens == (0, 1, 2, 4, 5)


def test_suppressed_tokens_never_decode_and_blanks_are_barred_only_at_the_start():
    prefers_blank_then_suppressed = _scripted([0, 6, 5, END])

    decoded = _decode(
        prefers_blank_then_suppressed,
        suppressed=np.array([5], dtype=np.int64),
        begin_suppressed=np.array([6], dtype=np.int64),
    )

    assert 6 not in decoded.tokens[:1]
    assert 5 not in decoded.tokens


class _Stopped(Exception):
    pass


def _resumable_backend(monkeypatch, recognized, stop_after=None):
    from exomem import onnx_asr_runtime

    window = onnx_asr_runtime.media_models.model_spec("speech")["sample_rate"] * 30
    monkeypatch.setattr(onnx_asr_runtime, "audio_chunks", lambda *_args: iter([np.full(window, 0.5, dtype=np.float32)] * 3))
    backend = onnx_asr_runtime.OnnxWhisperBackend()
    monkeypatch.setattr(backend, "_load", lambda: None)

    class _Speech:
        def segment_batch(self, *_args):
            return iter([iter([(0, 1)])])

    backend._vad = _Speech()

    def recognize(_waveform, language, seed):
        if stop_after is not None and len(recognized) == stop_after:
            raise _Stopped
        recognized.append(seed)
        return 50259, f"window {seed}"

    monkeypatch.setattr(backend, "_recognize", recognize)
    return backend, window


def test_a_stopped_transcript_resumes_after_its_last_finished_window(monkeypatch, vault, tmp_path):
    from exomem import onnx_asr_runtime

    audio = tmp_path / "talk.ogg"
    audio.write_bytes(b"thirty minutes of speech")
    identity = onnx_asr_runtime._file_sha256(audio)
    first = []
    backend, window = _resumable_backend(monkeypatch, first, stop_after=2)
    segments, _label = backend.transcribe(audio, vault_root=vault)
    with pytest.raises(_Stopped):
        list(segments)
    assert onnx_asr_runtime.progress_offset(vault, identity) == 2 * window

    second = []
    backend, _window = _resumable_backend(monkeypatch, second)
    segments, _label = backend.transcribe(audio, vault_root=vault)

    assert [segment.text for segment in segments] == ["window 0", "window 1", "window 2"]
    assert second == [2]
    assert onnx_asr_runtime.progress_offset(vault, identity) == 0


def test_progress_from_another_engine_revision_is_not_resumed(monkeypatch, vault, tmp_path):
    from exomem import onnx_asr_runtime

    audio = tmp_path / "talk.ogg"
    audio.write_bytes(b"thirty minutes of speech")
    backend, window = _resumable_backend(monkeypatch, [], stop_after=1)
    segments, _label = backend.transcribe(audio, vault_root=vault)
    with pytest.raises(_Stopped):
        list(segments)
    path = onnx_asr_runtime._progress_path(vault, onnx_asr_runtime._file_sha256(audio))
    record = json.loads(path.read_text())
    path.write_text(json.dumps({**record, "engine": "an earlier decoding revision"}))

    again = []
    backend, _window = _resumable_backend(monkeypatch, again)
    segments, _label = backend.transcribe(audio, vault_root=vault)

    assert [segment.text for segment in segments] == ["window 0", "window 1", "window 2"]
    assert again == [0, 1, 2]
