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


VOCAB = 12
END = 7
TS = 9  # token 8 is a special token between end-of-text and the first timestamp
POLICY = {
    "max_new_tokens": 40,
    "temperatures": [0.0, 0.2],
    "compression_ratio_threshold": 2.4,
    "logprob_threshold": -1.0,
    "trailing_speech_s": 2.0,
    "min_seek_s": 1.0,
}
RATE = 16000


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
        end_token=END, timestamp_begin=TS, max_initial_timestamp=VOCAB - TS - 1,
        detokenize=lambda tokens: " ".join(f"w{token}" for token in tokens),
        temperature=temperature, rng=np.random.default_rng(0),
    )
    options.update(overrides)
    return decode_window(step, [0], **options)


def test_a_repetition_loop_stops_before_the_token_cap():
    looping = _decode(_scripted([TS, 1, 2, *[3] * 60]))

    assert looping.compression_ratio > POLICY["compression_ratio_threshold"]
    assert len(looping.tokens) < POLICY["max_new_tokens"]


def test_fallback_keeps_the_first_attempt_that_is_neither_repetitive_nor_improbable():
    from exomem.onnx_asr_runtime import decode_with_fallback

    attempts = {0.0: _decode(_scripted([TS, 1, 2, *[3] * 60])), 0.2: _decode(_scripted([TS, 1, 2, 4, 5, END]))}

    chosen = decode_with_fallback(lambda temperature: attempts[temperature], POLICY)

    assert chosen.tokens == (TS, 1, 2, 4, 5)


def test_suppressed_tokens_never_decode_and_begin_suppression_applies_only_at_the_start():
    prefers_blank_then_suppressed = _scripted([TS + 1, 6, 5, END])

    decoded = _decode(
        prefers_blank_then_suppressed,
        suppressed=np.array([5], dtype=np.int64),
        begin_suppressed=np.array([TS + 1], dtype=np.int64),
    )

    assert decoded.tokens[0] != TS + 1
    assert 6 in decoded.tokens
    assert 5 not in decoded.tokens


def test_timestamp_rules_turn_text_first_and_a_backward_timestamp_into_a_valid_sequence():
    decoded = _decode(_scripted([3, TS + 1, 4, TS, 5, TS, END]))

    stamps = [token for token in decoded.tokens if token >= TS]
    assert decoded.tokens[0] >= TS
    assert stamps == sorted(stamps)


WINDOW = 30 * RATE
STEP = int(RATE * 0.02)


def _plan(tokens, *, speech=(), more_audio=True):
    from exomem.onnx_asr_runtime import plan_window

    return plan_window(
        tokens, timestamp_begin=TS, window_samples=WINDOW, rate=RATE, speech=list(speech), more_audio=more_audio,
        policy=POLICY,
    )


def test_a_window_that_ends_early_keeps_the_speech_that_follows_its_content():
    # [0.0] words [10.0]: one timestamp ends the decode while speech runs on to the window's end.
    plan = _plan([TS, 1, 2, TS + 500], speech=[(0, WINDOW)])

    assert plan.content_end == 500 * STEP
    assert plan.seek == 500 * STEP


def test_a_window_that_ends_early_with_no_speech_after_it_seeks_the_whole_window():
    plan = _plan([TS, 1, 2, TS + 500], speech=[(0, 500 * STEP + RATE)])

    assert plan.seek == WINDOW


def test_the_last_window_seeks_to_its_end_whatever_speech_remains():
    plan = _plan([TS, 1, 2, TS + 500], speech=[(0, WINDOW)], more_audio=False)

    assert plan.seek == WINDOW


def test_an_unfinished_segment_is_dropped_and_the_seek_stops_at_the_last_complete_timestamp():
    # (0.0 a 5.0)(5.0 b ...) the second segment never closes.
    plan = _plan([TS, 1, TS + 250, TS + 250, 2, 3], speech=[(0, WINDOW)])

    assert [segment.tokens for segment in plan.segments] == [(1,)]
    assert plan.seek == 250 * STEP


def test_a_window_whose_decode_makes_no_progress_still_advances_by_the_minimum_seek():
    plan = _plan([TS, 1, TS, TS, 2], speech=[(0, WINDOW)])

    assert plan.seek == int(POLICY["min_seek_s"] * RATE)


class _Stopped(Exception):
    pass


def _resumable_backend(monkeypatch, recognized, stop_after=None):
    from exomem import onnx_asr_runtime

    window = onnx_asr_runtime.media_models.model_spec("speech")["sample_rate"] * 30
    monkeypatch.setattr(onnx_asr_runtime, "audio_chunks", lambda *_args: iter([np.full(window, 0.5, dtype=np.float32)] * 3))
    backend = onnx_asr_runtime.OnnxWhisperBackend()
    monkeypatch.setattr(backend, "_load", lambda: None)
    backend._timestamp_begin = TS
    monkeypatch.setattr(backend, "_text", lambda tokens: f"window {tokens[0]}")

    class _Speech:
        def segment_batch(self, *_args):
            return iter([iter([(0, 1)])])

    backend._vad = _Speech()

    def recognize(_waveform, language, seed):
        if stop_after is not None and len(recognized) == stop_after:
            raise _Stopped
        recognized.append(seed)
        # One timestamp pair spanning the whole window; the text token names the window.
        return 50259, (TS, seed // window, TS + 1500)

    monkeypatch.setattr(backend, "_recognize", recognize)
    return backend, window


RECORDING = "Knowledge Base/Evidence/talk.ogg"


def _recording(vault):
    audio = vault / RECORDING
    audio.parent.mkdir(parents=True, exist_ok=True)
    audio.write_bytes(b"thirty minutes of speech")
    return audio


def test_a_stopped_transcript_resumes_after_its_last_finished_window(monkeypatch, vault):
    from exomem import onnx_asr_runtime

    audio = _recording(vault)
    first = []
    backend, window = _resumable_backend(monkeypatch, first, stop_after=2)
    segments, _label = backend.transcribe(audio, vault_root=vault)
    with pytest.raises(_Stopped):
        list(segments)
    assert onnx_asr_runtime.progress_offset(vault, RECORDING) == 2 * window

    second = []
    backend, _window = _resumable_backend(monkeypatch, second)
    segments, _label = backend.transcribe(audio, vault_root=vault)

    assert [segment.text for segment in segments] == ["window 0", "window 1", "window 2"]
    assert second == [2 * window]
    assert onnx_asr_runtime.progress_offset(vault, RECORDING) == 0


def test_progress_from_another_engine_revision_is_not_resumed(monkeypatch, vault):
    from exomem import onnx_asr_runtime

    audio = _recording(vault)
    backend, window = _resumable_backend(monkeypatch, [], stop_after=1)
    segments, _label = backend.transcribe(audio, vault_root=vault)
    with pytest.raises(_Stopped):
        list(segments)
    path = onnx_asr_runtime._progress_path(vault, RECORDING)
    record = json.loads(path.read_text())
    path.write_text(json.dumps({**record, "engine": "an earlier decoding revision"}))

    again = []
    backend, _window = _resumable_backend(monkeypatch, again)
    segments, _label = backend.transcribe(audio, vault_root=vault)

    assert [segment.text for segment in segments] == ["window 0", "window 1", "window 2"]
    assert again == [0, window, 2 * window]


def test_removing_a_recording_discards_its_partial_transcript(monkeypatch, vault):
    from exomem import index_sync, onnx_asr_runtime

    audio = _recording(vault)
    backend, window = _resumable_backend(monkeypatch, [], stop_after=1)
    segments, _label = backend.transcribe(audio, vault_root=vault)
    with pytest.raises(_Stopped):
        list(segments)
    assert onnx_asr_runtime.progress_offset(vault, RECORDING) == window

    audio.unlink()
    index_sync.delete_after_remove(vault, [RECORDING])

    assert onnx_asr_runtime.progress_offset(vault, RECORDING) == 0


@pytest.mark.parametrize("content", ["{not json", "[]", '{"format": 1}', '{"next_offset": "48000"}'])
def test_an_unreadable_progress_record_counts_as_no_progress(vault, content):
    from exomem import onnx_asr_runtime

    path = onnx_asr_runtime._progress_path(vault, RECORDING)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)

    assert onnx_asr_runtime.progress_offset(vault, RECORDING) == 0
