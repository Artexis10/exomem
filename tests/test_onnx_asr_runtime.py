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
