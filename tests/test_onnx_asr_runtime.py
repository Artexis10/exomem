"""Audio crosses the model window without losing its decoded tail."""

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
