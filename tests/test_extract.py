"""extract.py — media-type dispatch + soft-fail (engines themselves aren't run here)."""

from __future__ import annotations

import codecs
import json
import logging
import random
import sys
import types
from importlib.machinery import ModuleSpec
from pathlib import Path
from types import SimpleNamespace

import pytest

from exomem import (
    asr_runtime,
    extract,
    speaker_attribution,
    state_paths,
    voice_embed,
    voice_profiles,
)


@pytest.fixture(autouse=True)
def decoded_audio(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """These tests name audio files that do not exist; the decoder returns canned samples."""
    samples = [0.0, 0.25, -0.25]
    monkeypatch.setattr(extract, "_decode_audio_float32", lambda _path: samples)
    return samples


@pytest.mark.parametrize(
    "name,expected",
    [
        ("rec.mp3", "audio"),
        ("rec.WAV", "audio"),
        ("clip.mp4", "video"),
        ("clip.mov", "video"),
        ("shot.png", "image"),
        ("scan.JPG", "image"),
        ("doc.pdf", "pdf"),
        ("report.docx", "docx"),
        ("sheet.xlsx", "xlsx"),
        ("deck.pptx", "pptx"),
        ("page.HTML", "html"),
        ("notes.txt", "text"),
        ("mail.eml", "email"),
        ("cal.ics", "calendar"),
        ("archive.zip", None),
        ("noext", None),
    ],
)
def test_media_type_for(name: str, expected: str | None) -> None:
    assert extract.media_type_for(name) == expected


def test_is_extractable() -> None:
    assert extract.is_extractable("a.mp4") is True
    assert extract.is_extractable("a.docx") is True
    assert extract.is_extractable("a.zip") is False


def test_extraction_enabled_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", "1")
    assert extract.extraction_enabled() is False
    monkeypatch.delenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", raising=False)
    assert extract.extraction_enabled() is True


def test_prewarm_loads_the_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", raising=False)
    monkeypatch.setenv("EXOMEM_ASR_PREWARM", "1")
    called: list[bool] = []

    class _FakeTranscriber:
        def prewarm(self) -> None:
            called.append(True)

    monkeypatch.setattr(extract, "get_transcriber", lambda: _FakeTranscriber())
    extract.prewarm()
    assert called == [True]  # warmed eagerly


def test_prewarm_soft_fails_when_engine_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", raising=False)
    monkeypatch.setenv("EXOMEM_ASR_PREWARM", "1")

    class _UnavailableTranscriber:
        def prewarm(self) -> None:
            raise extract.ExtractionUnavailable("faster-whisper not installed")

    monkeypatch.setattr(extract, "get_transcriber", lambda: _UnavailableTranscriber())
    extract.prewarm()  # must not raise — a lean box just stays lazy


def test_prewarm_skipped_when_extraction_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", "1")
    called: list[bool] = []
    monkeypatch.setattr(extract, "_get_whisper", lambda: called.append(True))
    extract.prewarm()
    assert called == []  # disabled → never touches the model


def test_asr_prewarm_defaults_off_on_apple_silicon(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXOMEM_ASR_PREWARM", raising=False)
    monkeypatch.delenv("EXOMEM_ENABLE_ASR_PREWARM", raising=False)
    monkeypatch.delenv("EXOMEM_DISABLE_ASR_PREWARM", raising=False)
    monkeypatch.setattr(extract.sys, "platform", "darwin")
    monkeypatch.setattr(extract.platform, "machine", lambda: "arm64")

    assert extract.asr_prewarm_enabled() is False

    monkeypatch.setenv("EXOMEM_ASR_PREWARM", "1")
    assert extract.asr_prewarm_enabled() is True



def test_extract_text_routes_by_media_type(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        extract, "_transcribe",
        lambda p, mt, vault_root=None: extract.ExtractResult("T", mt, "whisper"),
    )
    monkeypatch.setattr(extract, "_ocr_image", lambda p: extract.ExtractResult("O", "image", "tesseract"))
    monkeypatch.setattr(extract, "_extract_pdf", lambda p: extract.ExtractResult("P", "pdf", "pymupdf"))
    monkeypatch.setattr(extract, "_extract_document", lambda p, mt: extract.ExtractResult("D", mt, "markitdown"))
    monkeypatch.setattr(extract, "_extract_textfile", lambda p: extract.ExtractResult("X", "text", "text"))
    monkeypatch.setattr(extract, "_extract_eml", lambda p: extract.ExtractResult("E", "email", "email"))
    monkeypatch.setattr(extract, "_extract_ics", lambda p: extract.ExtractResult("C", "calendar", "ics"))

    assert extract.extract_text("x.mp3").engine == "whisper"
    assert extract.extract_text("x.mp4").media_type == "video"
    assert extract.extract_text("x.png").engine == "tesseract"
    assert extract.extract_text("x.pdf").text == "P"
    assert extract.extract_text("x.docx").engine == "markitdown"
    assert extract.extract_text("x.xlsx").media_type == "xlsx"
    assert extract.extract_text("x.html").engine == "markitdown"
    assert extract.extract_text("x.txt").text == "X"
    assert extract.extract_text("x.eml").engine == "email"
    assert extract.extract_text("x.ics").media_type == "calendar"


@pytest.mark.parametrize(
    ("filename", "media_type"),
    [("recording.m4a", "audio"), ("recording.mp4", "video")],
)
def test_extract_text_explicit_timestamp_request_reaches_asr(
    filename: str,
    media_type: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, bool]] = []

    def _transcribe_spy(path, kind, vault_root=None, *, timestamps=False):
        seen.append((kind, timestamps))
        return extract.ExtractResult("[0:00] transcript", kind, "whisper+timed")

    monkeypatch.setattr(extract, "_transcribe", _transcribe_spy)

    result = extract.extract_text(filename, timestamps=True)

    assert result.media_type == media_type
    assert seen == [(media_type, True)]


def test_extract_text_default_does_not_request_timestamps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[bool] = []

    def _transcribe_spy(path, kind, vault_root=None, *, timestamps=False):
        seen.append(timestamps)
        return extract.ExtractResult("legacy transcript", kind, "whisper")

    monkeypatch.setattr(extract, "_transcribe", _transcribe_spy)

    result = extract.extract_text("recording.m4a")

    assert result.text == "legacy transcript"
    assert seen == [False]


def test_extract_text_unknown_type_raises() -> None:
    with pytest.raises(extract.ExtractionUnavailable):
        extract.extract_text("x.zip")


def test_whisper_uses_asr_runtime_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    made: list[dict] = []

    class _Model:
        pass

    monkeypatch.setitem(sys.modules, "faster_whisper", type("FW", (), {"WhisperModel": lambda *a, **k: made.append(k) or _Model})())
    monkeypatch.setattr(
        asr_runtime,
        "select_asr_runtime",
        lambda: asr_runtime.ASRSelection("cuda", "float16", asr_runtime.ASRProbe(True, frozenset({"float16"}), frozenset({"int8"}), (12, 0))),
    )
    monkeypatch.setattr(extract, "_WHISPER", None)

    extract._get_whisper()

    assert made == [{"device": "cuda", "compute_type": "float16", "cpu_threads": 1, "num_workers": 1}]


def test_constructor_cuda_failure_is_typed(monkeypatch: pytest.MonkeyPatch) -> None:
    class _BrokenModel:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("cuBLAS failed with status CUBLAS_STATUS_NOT_SUPPORTED")

    monkeypatch.setitem(sys.modules, "faster_whisper", type("FW", (), {"WhisperModel": _BrokenModel})())
    monkeypatch.setattr(
        asr_runtime,
        "select_asr_runtime",
        lambda: asr_runtime.ASRSelection("cuda", "float16", asr_runtime.ASRProbe(True, frozenset({"float16"}), frozenset({"int8"}), (12, 0))),
    )
    monkeypatch.setattr(extract, "_WHISPER", None)

    with pytest.raises(asr_runtime.ASRComputeRuntimeError):
        extract._get_whisper()


def test_lazy_segment_cuda_failure_is_typed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class _Model:
        def transcribe(self, _audio, **_options):
            def segments():
                raise RuntimeError("CUDA driver version is insufficient")
                yield None
            return segments(), None

    monkeypatch.setattr(extract, "_get_whisper", lambda: _Model())
    segments, _ = extract.FasterWhisperBackend().transcribe(tmp_path / "x.m4a")

    with pytest.raises(asr_runtime.ASRComputeRuntimeError):
        list(segments)


def test_extract_textfile_reads_utf8(tmp_path) -> None:
    f = tmp_path / "note.txt"
    f.write_text("plain text marker zylo", encoding="utf-8")
    r = extract._extract_textfile(f)
    assert r.media_type == "text" and r.engine == "text"
    assert "zylo" in r.text


@pytest.mark.parametrize(
    ("bom", "codec", "engine"),
    [
        (codecs.BOM_UTF16_LE, "utf-16-le", "text:utf-16"),
        (codecs.BOM_UTF16_BE, "utf-16-be", "text:utf-16"),
        # The UTF-32 LE mark begins with the UTF-16 LE mark; reading it as UTF-16 garbles every character.
        (codecs.BOM_UTF32_LE, "utf-32-le", "text:utf-32"),
        (codecs.BOM_UTF8, "utf-8", "text:utf-8-sig"),
    ],
)
def test_extract_textfile_reads_the_codec_a_byte_order_mark_names(tmp_path, bom, codec, engine) -> None:
    f = tmp_path / "minutes.txt"
    f.write_bytes(bom + "Minutes\nReference code cobalt-kettle-15851 applies.\n".encode(codec))
    r = extract.extract_text(f)
    assert r.text == "Minutes\nReference code cobalt-kettle-15851 applies."
    assert r.engine == engine


def test_extract_textfile_decodes_latin1_accents(tmp_path) -> None:
    body = "Brève réunion à la côte; señor Müller prévoit ½ prix, 20°C, façade ouverte."
    f = tmp_path / "notes.txt"
    f.write_bytes(f"{body}\nReference code saffron-kettle-49822 applies.\n".encode("latin-1"))
    r = extract.extract_text(f)
    assert body in r.text
    assert "saffron-kettle-49822" in r.text
    assert r.engine == "text:cp1252"


def test_extract_textfile_keeps_valid_characters_of_utf8_with_a_stray_byte(tmp_path) -> None:
    # A single-byte guess would read every valid multi-byte character of this text as two wrong ones.
    line = "Brève réunion à la côte; señor Müller prévoit ½ prix, 20°C, façade ouverte. Les élèves écoutent.\n"
    data = bytearray((line * 30).encode("utf-8"))
    data[len(data) // 2] = 0xFF
    f = tmp_path / "stray.txt"
    f.write_bytes(bytes(data))
    r = extract.extract_text(f)
    assert r.text == f.read_text(encoding="utf-8", errors="replace").strip()
    assert r.text.count("Brève réunion") >= 29 and r.engine == "text"


def test_extract_textfile_replaces_what_no_codec_explains_as_before(tmp_path) -> None:
    noise = random.Random(0).randbytes(2000)
    f = tmp_path / "noise.txt"
    f.write_bytes(noise)
    r = extract.extract_text(f)
    assert r.text == f.read_text(encoding="utf-8", errors="replace").strip()
    assert r.engine == "text"


def test_extract_ics_decodes_a_utf16_calendar(tmp_path) -> None:
    ics = "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nSUMMARY:Réunion Appsignal 7731\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    f = tmp_path / "e.ics"
    f.write_bytes(codecs.BOM_UTF16_LE + ics.encode("utf-16-le"))
    assert "Réunion Appsignal 7731" in extract.extract_text(f).text


def test_extract_eml_pulls_headers_and_body(tmp_path) -> None:
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["From"] = "a@example.com"
    msg["Subject"] = "Quokka invoice 7731"
    msg.set_content("body marker narwhal")
    p = tmp_path / "m.eml"
    p.write_bytes(msg.as_bytes())
    r = extract._extract_eml(p)
    assert "Quokka invoice 7731" in r.text  # subject header
    assert "narwhal" in r.text              # body
    assert r.media_type == "email"


def test_extract_ics_pulls_vevent_fields(tmp_path) -> None:
    ics = (
        "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\n"
        "SUMMARY:Appsignal Catchup 7731\r\n"
        "DTSTART:20260513T153000\r\n"
        "LOCATION:TLN-Roseni-3\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    f = tmp_path / "e.ics"
    f.write_text(ics, encoding="utf-8")
    r = extract._extract_ics(f)
    assert "Appsignal Catchup 7731" in r.text
    assert "TLN-Roseni-3" in r.text
    assert r.media_type == "calendar"


def test_unreadable_document_fails_as_the_file_not_as_a_missing_engine(tmp_path) -> None:
    # automatic-media-processing: an unreadable artifact fails with its own reason. Read as
    # a missing engine, it would block and requeue at every start once the engine exists.
    pytest.importorskip("markitdown")
    with pytest.raises(FileNotFoundError):
        extract._extract_document(tmp_path / "does-not-exist.docx", "docx")


# ---------------- optional: ASR speaker diarization (EXOMEM_DIARIZE, default OFF) ----


class _FakeSeg:
    def __init__(self, text: str, start: float, end: float) -> None:
        self.text = text
        self.start = start
        self.end = end


class _FakeWhisper:
    def __init__(
        self, segs: list, *, info: object | None = None, supported=("en", "et"), device: str = "cpu"
    ) -> None:
        self._segs = segs
        self._info = info
        self.supported_languages = supported
        self.model = SimpleNamespace(device=device)  # where CTranslate2 reports that it runs
        self.calls: list[tuple[object, dict]] = []

    def transcribe(self, audio, **options):  # faster-whisper returns (segments_generator, info)
        self.calls.append((audio, options))
        return iter(self._segs), self._info


def test_diarize_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXOMEM_DIARIZE", raising=False)
    assert extract._diarize_enabled() is False
    monkeypatch.setenv("EXOMEM_DIARIZE", "1")
    assert extract._diarize_enabled() is True


def test_transcribe_plain_when_diarize_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXOMEM_DIARIZE", raising=False)
    segs = [_FakeSeg("hello there", 0.0, 1.0), _FakeSeg("general kenobi", 1.0, 2.0)]
    monkeypatch.setattr(extract, "_get_whisper", lambda: _FakeWhisper(segs))
    r = extract._transcribe(Path("x.wav"), "audio")
    assert r.text == "hello there general kenobi"
    assert r.speakers is None
    assert r.engine == f"faster-whisper:{extract.WHISPER_MODEL}"


@pytest.fixture
def batched_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[object, object, dict]]:
    """faster-whisper's batched pipeline, recording the model, audio and options it is given."""
    calls: list[tuple[object, object, dict]] = []

    class _Pipeline:
        def __init__(self, model) -> None:
            self.model = model

        def transcribe(self, audio, **options):
            calls.append((self.model, audio, options))
            failure = getattr(self.model, "batched_failure", None)

            def decode():
                if failure is not None:  # the engine fails when it decodes the first batch
                    raise failure
                yield from self.model._segs

            return decode(), self.model._info

    module = types.ModuleType("faster_whisper")
    module.__spec__ = ModuleSpec("faster_whisper", loader=None)  # engine selection asks importlib for it
    module.BatchedInferencePipeline = _Pipeline
    monkeypatch.setitem(sys.modules, "faster_whisper", module)
    return calls


def test_a_gpu_decodes_in_batches_and_keeps_segment_timestamps(
    monkeypatch: pytest.MonkeyPatch, decoded_audio: list[float], batched_calls: list
) -> None:
    from exomem import media_models

    monkeypatch.delenv(extract.ASR_LANGUAGE_ENV, raising=False)
    info = SimpleNamespace(language="et", language_probability=0.91, duration=61.5, duration_after_vad=40.25)
    model = _FakeWhisper([_FakeSeg("tere", 0.0, 4.0)], info=info, device="cuda")
    monkeypatch.setattr(extract, "_get_whisper", lambda: model)

    result = extract._transcribe(Path("x.wav"), "audio")

    policy = media_models.model_spec("speech")["faster_whisper"]
    ((used, audio, options),) = batched_calls
    assert used is model and audio is decoded_audio
    assert options == {
        "language": None, "multilingual": False, "batch_size": policy["batched"]["batch_size"],
        **policy["batched"]["options"],
    }
    assert options["without_timestamps"] is False  # the pipeline's own default gives one line per window
    assert model.calls == []
    assert result.text == "tere" and result.asr.policy == policy["revision"]
    assert result.asr.decoding == "batched"


def test_a_gpu_too_small_for_the_batch_decodes_the_file_sequentially_and_records_it(
    monkeypatch: pytest.MonkeyPatch, batched_calls: list
) -> None:
    from exomem import media_models

    model = _FakeWhisper([_FakeSeg("tere", 0.0, 4.0)], device="cuda")
    model.batched_failure = RuntimeError("CUDA failed with error out of memory")
    monkeypatch.setattr(extract, "_get_whisper", lambda: model)

    result = extract._transcribe(Path("x.wav"), "audio")

    policy = media_models.model_spec("speech")["faster_whisper"]
    assert len(batched_calls) == 1  # the batched pipeline was tried first
    assert model.calls[0][1] == {"language": None, "multilingual": False, **policy["options"]}
    assert result.text == "tere" and result.asr.decoding == "sequential"


def test_a_cpu_run_decodes_sequentially_even_where_the_batched_pipeline_exists(
    monkeypatch: pytest.MonkeyPatch, batched_calls: list
) -> None:
    from exomem import media_models

    model = _FakeWhisper([_FakeSeg("tere", 0.0, 4.0)], device="cpu")
    monkeypatch.setattr(extract, "_get_whisper", lambda: model)

    result = extract._transcribe(Path("x.wav"), "audio")

    policy = media_models.model_spec("speech")["faster_whisper"]
    assert batched_calls == []
    assert model.calls[0][1] == {"language": None, "multilingual": False, **policy["options"]}
    assert result.asr.decoding == "sequential"


@pytest.mark.parametrize("device", ["cpu", "cuda"])
def test_the_multilingual_mode_reaches_the_engine_in_either_pipeline_and_is_recorded(
    monkeypatch: pytest.MonkeyPatch, batched_calls: list, device: str
) -> None:
    monkeypatch.setenv(extract.ASR_LANGUAGE_ENV, "fi")  # the mode is a request, so it beats the default
    info = SimpleNamespace(language="et", language_probability=0.6, duration=30.0, duration_after_vad=20.0)
    model = _FakeWhisper([_FakeSeg("tere", 0.0, 4.0)], info=info, device=device)
    monkeypatch.setattr(extract, "_get_whisper", lambda: model)

    result = extract.extract_text(Path("x.wav"), media_type="audio", multilingual=True)

    options = batched_calls[0][2] if device == "cuda" else model.calls[0][1]
    assert options["multilingual"] is True and options["language"] is None
    facts = result.asr
    assert (facts.language_mode, facts.language_source, facts.language) == ("multilingual", "requested", "et")
    assert facts.language_requested is None and facts.language_probability is None


def test_a_declared_hallucination_guard_travels_with_the_word_timestamps_it_needs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import copy

    from exomem import media_models

    spec = copy.deepcopy(media_models.model_spec("speech"))
    spec["faster_whisper"]["options"]["hallucination_silence_threshold"] = 2.0
    monkeypatch.setattr(media_models, "model_spec", lambda _name: spec)
    model = _FakeWhisper([_FakeSeg("tere", 0.0, 4.0)])
    monkeypatch.setattr(extract, "_get_whisper", lambda: model)

    extract._transcribe(Path("x.wav"), "audio")

    options = model.calls[0][1]
    assert options["hallucination_silence_threshold"] == 2.0 and options["word_timestamps"] is True


@pytest.mark.parametrize("engine", ["mlx", "onnx"])
def test_an_engine_that_reads_one_language_per_file_refuses_the_multilingual_mode(engine: str) -> None:
    from exomem import onnx_asr_runtime

    backend = extract.MlxWhisperBackend() if engine == "mlx" else onnx_asr_runtime.OnnxWhisperBackend()

    with pytest.raises(extract.MultilingualUnsupported):
        backend.transcribe(Path("x.wav"), multilingual=True)


def test_the_speech_policy_reaches_the_model_and_the_engine_decisions_are_recorded(
    monkeypatch: pytest.MonkeyPatch, decoded_audio: list[float]
) -> None:
    from types import SimpleNamespace

    from exomem import asr_facts, media_models

    monkeypatch.delenv(extract.ASR_LANGUAGE_ENV, raising=False)
    info = SimpleNamespace(
        language="et", language_probability=0.91, duration=61.5, duration_after_vad=40.25
    )
    model = _FakeWhisper([_FakeSeg("tere", 0.0, 1.0)], info=info)
    monkeypatch.setattr(extract, "_get_whisper", lambda: model)

    result = extract._transcribe(Path("x.wav"), "audio")

    policy = media_models.model_spec("speech")["faster_whisper"]
    audio, options = model.calls[0]
    assert audio is decoded_audio
    assert options == {"language": None, "multilingual": False, **policy["options"]}
    assert options["vad_filter"] is True  # language is detected on speech, not on a quiet opening
    assert result.asr == asr_facts.AsrFacts(
        language="et", language_probability=0.91, language_source="detected", language_mode="single",
        decoding="sequential", speech_seconds=40.25, duration_seconds=61.5, policy=policy["revision"],
    )


@pytest.mark.parametrize(
    "requested,configured,forced,source",
    [
        ("et", "fi", "et", "requested"),
        (None, "fi", "fi", "configured"),
        (None, None, None, "detected"),
    ],
)
def test_a_requested_language_beats_the_configured_one_and_both_beat_detection(
    monkeypatch: pytest.MonkeyPatch, requested, configured, forced, source
) -> None:
    from types import SimpleNamespace

    if configured:
        monkeypatch.setenv(extract.ASR_LANGUAGE_ENV, configured)
    else:
        monkeypatch.delenv(extract.ASR_LANGUAGE_ENV, raising=False)
    info = SimpleNamespace(
        language=forced or "en", language_probability=1.0 if forced else 0.8,
        duration=3.0, duration_after_vad=2.0,
    )
    model = _FakeWhisper([_FakeSeg("tere", 0.0, 1.0)], info=info, supported=("en", "et", "fi"))
    monkeypatch.setattr(extract, "_get_whisper", lambda: model)

    result = extract.extract_text(Path("x.wav"), media_type="audio", language=requested)

    assert model.calls[0][1]["language"] == forced
    assert result.asr.language_source == source
    assert result.asr.language_requested == (forced if source == "requested" else None)
    # A language that was asked for was not measured, so it carries no probability.
    assert (result.asr.language_probability is None) == (source != "detected")


def test_a_language_the_model_does_not_publish_is_refused_not_replaced_by_detection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = _FakeWhisper([_FakeSeg("tere", 0.0, 1.0)], supported=("en", "et"))
    monkeypatch.setattr(extract, "_get_whisper", lambda: model)

    with pytest.raises(extract.UnsupportedLanguage) as refused:
        extract._transcribe(Path("x.wav"), "audio", language="xx")

    assert refused.value.code == "xx" and refused.value.supported == ["en", "et"]
    assert model.calls == []


def test_pdf_text_names_each_page_including_a_page_read_by_ocr(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import types

    class _Page:
        def __init__(self, text: str) -> None:
            self._text = text

        def get_text(self) -> str:
            return self._text

    class _Document(list):
        def __enter__(self):
            return self

        def __exit__(self, *_exc) -> None:
            return None

    pages = _Document([_Page("The first page holds enough text."), _Page(""), _Page("The third page holds more text.")])
    monkeypatch.setitem(
        sys.modules, "fitz", types.SimpleNamespace(open=lambda _path: pages)
    )
    monkeypatch.setattr(extract, "_ocr_pdf_page", lambda _page: "scanned words")

    result = extract._extract_pdf(tmp_path / "case.pdf")

    assert result.text == (
        "[page 1]\nThe first page holds enough text.\n\n"
        "[page 2]\nscanned words\n\n"
        "[page 3]\nThe third page holds more text."
    )


def test_transcribe_labels_speakers_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_DIARIZE", "1")
    segs = [_FakeSeg("hello there", 0.0, 1.0), _FakeSeg("general kenobi", 1.0, 2.0)]
    monkeypatch.setattr(extract, "_get_whisper", lambda: _FakeWhisper(segs))
    # Stub the raw diarization → two turns mapped to distinct speakers (no real model).
    monkeypatch.setattr(
        extract, "_run_diarization",
        lambda p: [(0.0, 1.0, "SPEAKER_00"), (1.0, 2.0, "SPEAKER_01")],
    )
    r = extract._transcribe(Path("x.wav"), "audio")
    assert "[Speaker A]: hello there" in r.text
    assert "[Speaker B]: general kenobi" in r.text
    assert r.engine.endswith("+diarized")
    assert r.speakers is not None
    assert [t["speaker"] for t in r.speakers] == ["Speaker A", "Speaker B"]
    assert r.speakers[0]["text"] == "hello there"


def test_transcribe_merges_consecutive_same_speaker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_DIARIZE", "1")
    segs = [_FakeSeg("part one", 0.0, 1.0), _FakeSeg("part two", 1.0, 2.0)]
    monkeypatch.setattr(extract, "_get_whisper", lambda: _FakeWhisper(segs))
    monkeypatch.setattr(
        extract, "_run_diarization",
        lambda p: [(0.0, 2.0, "SPEAKER_00")],  # one speaker the whole time
    )
    r = extract._transcribe(Path("x.wav"), "audio")
    assert r.text == "[Speaker A]: part one part two"
    assert len(r.speakers) == 1


def test_transcribe_soft_fails_to_plain_when_diarization_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EXOMEM_DIARIZE", "1")
    segs = [_FakeSeg("solo line", 0.0, 1.0)]
    monkeypatch.setattr(extract, "_get_whisper", lambda: _FakeWhisper(segs))

    # Sidecar venv not provisioned → real _run_diarization runs its locate-then-spawn path,
    # finds no sidecar interpreter, and soft-fails to the plain transcript.
    monkeypatch.setattr(extract, "_diarizer_sidecar_python", lambda: None)
    r = extract._transcribe(Path("x.wav"), "audio")
    assert r.text == "solo line"
    assert r.speakers is None
    assert r.engine == f"faster-whisper:{extract.WHISPER_MODEL}"


# ---------------- optional: timed transcripts (EXOMEM_SEMANTIC_SEGMENTS, default OFF) ----


def test_transcribe_gate_off_is_byte_identical_plain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXOMEM_SEMANTIC_SEGMENTS", raising=False)
    monkeypatch.delenv("EXOMEM_DIARIZE", raising=False)
    segs = [_FakeSeg("hello there", 0.0, 1.0), _FakeSeg("general kenobi", 1.0, 2.0)]
    monkeypatch.setattr(extract, "_get_whisper", lambda: _FakeWhisper(segs))
    r = extract._transcribe(Path("x.wav"), "audio")
    assert r.text == "hello there general kenobi"
    assert r.engine == f"faster-whisper:{extract.WHISPER_MODEL}"


def test_transcribe_timed_plain_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_SEMANTIC_SEGMENTS", "1")
    monkeypatch.delenv("EXOMEM_DIARIZE", raising=False)
    segs = [_FakeSeg("hello there", 5.0, 8.0), _FakeSeg("general kenobi", 3080.0, 3084.0)]
    monkeypatch.setattr(extract, "_get_whisper", lambda: _FakeWhisper(segs))
    r = extract._transcribe(Path("x.wav"), "audio")
    assert r.text == "[0:05] hello there\n[51:20] general kenobi"
    assert r.engine == f"faster-whisper:{extract.WHISPER_MODEL}+timed"
    assert r.speakers is None


def test_transcribe_explicit_timestamps_override_environment_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EXOMEM_SEMANTIC_SEGMENTS", raising=False)
    monkeypatch.delenv("EXOMEM_DIARIZE", raising=False)
    segs = [_FakeSeg("automatic transcript", 65.0, 67.0)]
    monkeypatch.setattr(extract, "_get_whisper", lambda: _FakeWhisper(segs))

    result = extract._transcribe(Path("automatic.m4a"), "audio", timestamps=True)

    assert result.text == "[1:05] automatic transcript"
    assert result.engine == f"faster-whisper:{extract.WHISPER_MODEL}+timed"


def test_transcribe_diarization_records_anonymous_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EXOMEM_SEMANTIC_SEGMENTS", raising=False)
    monkeypatch.setenv("EXOMEM_DIARIZE", "1")
    segs = [_FakeSeg("unknown participant", 0.0, 1.0)]
    monkeypatch.setattr(extract, "_get_whisper", lambda: _FakeWhisper(segs))
    monkeypatch.setattr(
        extract, "_run_diarization", lambda _path: [(0.0, 1.0, "SPEAKER_00")]
    )
    monkeypatch.setattr(extract, "_resolve_named_labels", lambda *_a, **_kw: None)

    result = extract._transcribe(Path("anonymous.m4a"), "audio", timestamps=True)

    assert result.text == "[0:00] [Speaker A]: unknown participant"
    assert result.speaker_verification == "anonymous"


def test_transcribe_profile_label_is_only_used_when_resolver_returns_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EXOMEM_SEMANTIC_SEGMENTS", raising=False)
    monkeypatch.setenv("EXOMEM_DIARIZE", "1")
    segs = [_FakeSeg("known participant", 0.0, 1.0)]
    monkeypatch.setattr(extract, "_get_whisper", lambda: _FakeWhisper(segs))
    monkeypatch.setattr(
        extract, "_run_diarization", lambda _path: [(0.0, 1.0, "SPEAKER_00")]
    )
    monkeypatch.setattr(
        extract,
        "_resolve_named_labels",
        lambda *_a, **_kw: {"SPEAKER_00": "Enrolled Speaker"},
    )

    result = extract._transcribe(Path("profile.m4a"), "audio", timestamps=True)

    assert result.text == "[0:00] [Enrolled Speaker]: known participant"
    assert result.speaker_verification == "profile-matched"


@pytest.mark.parametrize(
    ("label", "matched_profile", "verification"),
    [
        ("Speaker A", "Speaker A", "profile-matched"),
        ("Speaker 27", None, "anonymous"),
    ],
)
def test_speaker_verification_uses_resolver_match_metadata_not_label_shape(
    label: str,
    matched_profile: str | None,
    verification: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EXOMEM_DIARIZE", "1")
    segs = [_FakeSeg("speaker line", 0.0, 1.0)]
    monkeypatch.setattr(extract, "_get_whisper", lambda: _FakeWhisper(segs))
    monkeypatch.setattr(
        extract, "_run_diarization", lambda _path: [(0.0, 1.0, "SPEAKER_00")]
    )
    monkeypatch.setattr(
        voice_profiles,
        "voice_profiles_path",
        lambda root: state_paths.vault_state_dir(root) / "profiles",
    )
    monkeypatch.setattr(
        voice_profiles,
        "load_profiles",
        lambda _path, **_kwargs: {"profile": object()},
    )
    monkeypatch.setattr(voice_embed, "embed_spans", lambda *_a, **_kw: object())
    monkeypatch.setattr(
        speaker_attribution,
        "attribute_clusters",
        lambda *_a, **_kw: {
            "SPEAKER_00": speaker_attribution.Attribution(label, 0.9, matched_profile)
        },
    )

    result = extract._transcribe(
        Path("metadata.m4a"), "audio", vault_root=tmp_path, timestamps=True
    )

    assert f"[{label}]: speaker line" in result.text
    assert result.speaker_verification == verification


def test_transcribe_accepts_legacy_two_tuple_diarization_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EXOMEM_DIARIZE", "1")
    segs = [_FakeSeg("legacy seam", 0.0, 1.0)]
    speakers = [{"speaker": "Speaker A", "start": 0.0, "end": 1.0, "text": "legacy seam"}]
    monkeypatch.setattr(extract, "_get_whisper", lambda: _FakeWhisper(segs))
    monkeypatch.setattr(
        extract,
        "_diarize",
        lambda *_a, **_kw: ("[0:00] [Speaker A]: legacy seam", speakers),
    )

    result = extract._transcribe(Path("legacy.m4a"), "audio", timestamps=True)

    assert result.speaker_verification == "anonymous"


def test_transcribe_records_unavailable_when_configured_diarization_cannot_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EXOMEM_SEMANTIC_SEGMENTS", raising=False)
    monkeypatch.setenv("EXOMEM_DIARIZE", "1")
    segs = [_FakeSeg("plain fallback", 2.0, 3.0)]
    monkeypatch.setattr(extract, "_get_whisper", lambda: _FakeWhisper(segs))
    monkeypatch.setattr(extract, "_diarizer_sidecar_python", lambda: None)

    result = extract._transcribe(Path("no-diarizer.m4a"), "audio", timestamps=True)

    assert result.text == "[0:02] plain fallback"
    assert result.engine == f"faster-whisper:{extract.WHISPER_MODEL}+timed"
    assert result.speaker_verification == "unavailable"


def test_transcribe_timed_diarized_per_segment_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_SEMANTIC_SEGMENTS", "1")
    monkeypatch.setenv("EXOMEM_DIARIZE", "1")
    segs = [
        _FakeSeg("part one", 0.0, 1.0),
        _FakeSeg("part two", 1.0, 2.0),
        _FakeSeg("reply", 2.0, 3.0),
    ]
    monkeypatch.setattr(extract, "_get_whisper", lambda: _FakeWhisper(segs))
    monkeypatch.setattr(
        extract, "_run_diarization",
        lambda p: [(0.0, 2.0, "SPEAKER_00"), (2.0, 3.0, "SPEAKER_01")],
    )
    r = extract._transcribe(Path("x.wav"), "audio")
    # Per-segment timed lines with the label repeated — NOT merged turns.
    assert r.text.splitlines() == [
        "[0:00] [Speaker A]: part one",
        "[0:01] [Speaker A]: part two",
        "[0:02] [Speaker B]: reply",
    ]
    # Suffix order is load-bearing: _needs_rediarize matches endswith("+diarized").
    assert r.engine == f"faster-whisper:{extract.WHISPER_MODEL}+timed+diarized"
    # The structured speakers list keeps the MERGED-turn shape (unchanged surface).
    assert [t["speaker"] for t in r.speakers] == ["Speaker A", "Speaker B"]
    assert r.speakers[0]["text"] == "part one part two"


def test_transcribe_gate_off_diarized_byte_identical(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXOMEM_SEMANTIC_SEGMENTS", raising=False)
    monkeypatch.setenv("EXOMEM_DIARIZE", "1")
    segs = [_FakeSeg("part one", 0.0, 1.0), _FakeSeg("part two", 1.0, 2.0)]
    monkeypatch.setattr(extract, "_get_whisper", lambda: _FakeWhisper(segs))
    monkeypatch.setattr(extract, "_run_diarization", lambda p: [(0.0, 2.0, "SPEAKER_00")])
    r = extract._transcribe(Path("x.wav"), "audio")
    assert r.text == "[Speaker A]: part one part two"
    assert r.engine == f"faster-whisper:{extract.WHISPER_MODEL}+diarized"


def test_transcribe_timed_survives_diarization_soft_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EXOMEM_SEMANTIC_SEGMENTS", "1")
    monkeypatch.setenv("EXOMEM_DIARIZE", "1")
    segs = [_FakeSeg("solo line", 5.0, 6.0)]
    monkeypatch.setattr(extract, "_get_whisper", lambda: _FakeWhisper(segs))
    monkeypatch.setattr(extract, "_diarizer_sidecar_python", lambda: None)
    r = extract._transcribe(Path("x.wav"), "audio")
    assert r.text == "[0:05] solo line"  # timed plain, no speaker labels
    assert r.engine == f"faster-whisper:{extract.WHISPER_MODEL}+timed"


def test_transcribe_timed_render_failure_falls_back_flat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EXOMEM_SEMANTIC_SEGMENTS", "1")
    monkeypatch.delenv("EXOMEM_DIARIZE", raising=False)
    segs = [_FakeSeg("hello there", 0.0, 1.0)]
    monkeypatch.setattr(extract, "_get_whisper", lambda: _FakeWhisper(segs))
    class _BoomSemanticSegments:
        @staticmethod
        def render_timed_lines(_segments):
            raise RuntimeError("boom")

    monkeypatch.setattr(
        extract, "_semantic_segments_module", lambda: _BoomSemanticSegments
    )
    r = extract._transcribe(Path("x.wav"), "audio")
    assert r.text == "hello there"
    assert r.engine == f"faster-whisper:{extract.WHISPER_MODEL}"  # no lying +timed marker


def test_explicit_timestamp_render_failure_does_not_return_untimed_transcript(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EXOMEM_SEMANTIC_SEGMENTS", raising=False)
    monkeypatch.delenv("EXOMEM_DIARIZE", raising=False)
    segs = [_FakeSeg("must stay timed", 0.0, 1.0)]
    monkeypatch.setattr(extract, "_get_whisper", lambda: _FakeWhisper(segs))

    class _BoomSemanticSegments:
        @staticmethod
        def render_timed_lines(_segments):
            raise RuntimeError("renderer exploded")

    monkeypatch.setattr(extract, "_semantic_segments_module", lambda: _BoomSemanticSegments)

    with pytest.raises(
        extract.TimestampRenderingUnavailable, match="timed transcript rendering failed"
    ):
        extract._transcribe(Path("automatic.m4a"), "audio", timestamps=True)


def test_explicit_timestamp_transcription_treats_silent_audio_as_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EXOMEM_SEMANTIC_SEGMENTS", raising=False)
    monkeypatch.delenv("EXOMEM_DIARIZE", raising=False)
    monkeypatch.setattr(extract, "_get_whisper", lambda: _FakeWhisper([]))

    result = extract._transcribe(Path("silent.m4a"), "audio", timestamps=True)

    assert result.text == "[0:00] (no speech detected)"
    assert result.engine == f"faster-whisper:{extract.WHISPER_MODEL}+timed"


# ---------------- optional: vision captioning (EXOMEM_VISION_CAPTION, default OFF) ----


def test_vision_caption_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXOMEM_VISION_CAPTION", raising=False)
    assert extract._vision_caption_enabled() is False
    monkeypatch.setenv("EXOMEM_VISION_CAPTION", "1")
    assert extract._vision_caption_enabled() is True


def test_maybe_caption_ocr_only_when_flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXOMEM_VISION_CAPTION", raising=False)
    called: list = []
    monkeypatch.setattr(extract, "_caption_image", lambda p: called.append(p) or "nope")
    text, engine = extract._maybe_caption("ocr body", Path("x.png"))
    assert text == "ocr body"
    assert engine == "tesseract"
    assert called == []  # flag off → captioner is never invoked


def test_maybe_caption_prepends_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_VISION_CAPTION", "1")
    monkeypatch.setattr(extract, "_caption_image", lambda p: "a cat sitting on a mat")
    text, engine = extract._maybe_caption("INVOICE 7731", Path("x.png"))
    assert text == "a cat sitting on a mat\n\nINVOICE 7731"
    assert engine.startswith("tesseract+")


def test_maybe_caption_empty_ocr_returns_caption_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_VISION_CAPTION", "1")
    monkeypatch.setattr(extract, "_caption_image", lambda p: "a beach at sunset")
    text, engine = extract._maybe_caption("", Path("x.png"))
    assert text == "a beach at sunset"
    assert engine.startswith("tesseract+")


def test_maybe_caption_soft_fails_to_ocr_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_VISION_CAPTION", "1")
    monkeypatch.setattr(extract, "_CAPTIONER", None)

    def _no_dep():
        raise ImportError("transformers not installed")

    # Real _caption_image runs; its model loader raises ImportError → soft-fail.
    monkeypatch.setattr(extract, "_load_captioner", _no_dep)
    text, engine = extract._maybe_caption("ocr body", Path("x.png"))
    assert text == "ocr body"
    assert engine == "tesseract"


# ---------------- opt-in env-flag parse + diarization readiness diagnostics ----------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("1", True), ("true", True), ("yes", True), ("on", True), ("anything", True),
        ("0", False), ("false", False), ("FALSE", False), ("no", False),
        ("off", False), ("Off", False), ("", False), ("  ", False),
    ],
)
def test_env_flag_truthy_parse(monkeypatch: pytest.MonkeyPatch, value: str, expected: bool) -> None:
    # A bare presence check would read `EXOMEM_DIARIZE=0` as opted IN — falsy values
    # must count as unset, for both opt-in flags.
    monkeypatch.setenv("EXOMEM_DIARIZE", value)
    assert extract._diarize_enabled() is expected
    monkeypatch.setenv("EXOMEM_VISION_CAPTION", value)
    assert extract._vision_caption_enabled() is expected


def _readiness_record(caplog: pytest.LogCaptureFixture) -> logging.LogRecord:
    return next(r for r in caplog.records if "diarization readiness" in r.getMessage())


def test_readiness_healthy_logs_info_with_profiles(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    monkeypatch.setenv("EXOMEM_DIARIZE", "1")
    monkeypatch.setenv("HUGGINGFACE_TOKEN", "hf_secret_value_123")
    monkeypatch.setattr(extract, "_diarizer_sidecar_python", lambda: Path(sys.executable))
    monkeypatch.setattr(
        voice_profiles,
        "load_profiles",
        lambda _path, **_kwargs: {"Fixture Speaker": object(), "Maria": object()},
    )
    with caplog.at_level(logging.INFO, logger="exomem.extract"):
        extract.log_diarization_readiness(tmp_path)
    rec = _readiness_record(caplog)
    assert rec.levelno == logging.INFO
    msg = rec.getMessage()
    assert "enabled=True" in msg and "sidecar_venv=True" in msg and "hf_token=True" in msg
    assert "profiles=2" in msg and "Fixture Speaker" in msg and "Maria" in msg
    assert "hf_secret_value_123" not in caplog.text  # token presence only, never the value


def test_readiness_warns_when_enabled_but_venv_missing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    monkeypatch.setenv("EXOMEM_DIARIZE", "1")
    monkeypatch.setenv("HUGGINGFACE_TOKEN", "x")
    monkeypatch.setattr(extract, "_diarizer_sidecar_python", lambda: None)
    with caplog.at_level(logging.INFO, logger="exomem.extract"):
        extract.log_diarization_readiness(tmp_path)
    assert _readiness_record(caplog).levelno == logging.WARNING


def test_readiness_disabled_logs_info(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    # A lean box with diarization off must not be nagged with warnings.
    monkeypatch.delenv("EXOMEM_DIARIZE", raising=False)
    monkeypatch.delenv("HUGGINGFACE_TOKEN", raising=False)
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.setattr(extract, "_diarizer_sidecar_python", lambda: None)
    with caplog.at_level(logging.INFO, logger="exomem.extract"):
        extract.log_diarization_readiness(tmp_path)
    rec = _readiness_record(caplog)
    assert rec.levelno == logging.INFO
    assert "enabled=False" in rec.getMessage()


def test_readiness_never_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("EXOMEM_DIARIZE", "1")

    def _boom(p):
        raise RuntimeError("profile store exploded")

    monkeypatch.setattr(voice_profiles, "load_profiles", _boom)
    extract.log_diarization_readiness(tmp_path)  # must not raise (prewarm contract)


def test_transcribe_soft_fails_when_diarize_raises(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Spec: Soft-Fail Degradation — an exception ANYWHERE in the optional diarization
    # layer must degrade to the plain transcript, never break extraction. (A mid-run
    # source change once escaped via an unguarded import inside _diarize.)
    monkeypatch.setenv("EXOMEM_DIARIZE", "1")
    segs = [_FakeSeg("solo line", 0.0, 1.0)]
    monkeypatch.setattr(extract, "_get_whisper", lambda: _FakeWhisper(segs))

    def _boom(path, seg_list, vault_root=None):
        raise RuntimeError("import exploded mid-run")

    monkeypatch.setattr(extract, "_diarize", _boom)
    with caplog.at_level(logging.WARNING, logger="exomem.extract"):
        r = extract._transcribe(Path("x.wav"), "audio")
    assert r.text == "solo line"
    assert not r.engine.endswith("+diarized")
    assert r.speakers is None
    assert any("diarization failed" in rec.getMessage() for rec in caplog.records)


def test_resolve_named_labels_prefers_explicit_vault_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A caller-supplied vault_root must reach the profile store WITHOUT env resolution —
    # a CLI back-fill run with only --vault has no EXOMEM_VAULT_PATH exported.
    from exomem import vault as vault_module

    seen: list[Path] = []

    def _env_resolve_must_not_run():
        raise AssertionError("env vault resolution must not be consulted")

    monkeypatch.setattr(vault_module, "resolve_vault", _env_resolve_must_not_run)

    def _spy_path(root):
        seen.append(root)
        return state_paths.vault_state_dir(root) / ".voice_profiles.json"

    monkeypatch.setattr(voice_profiles, "voice_profiles_path", _spy_path)
    monkeypatch.setattr(voice_profiles, "load_profiles", lambda _path, **_kwargs: {})
    out = extract._resolve_named_labels(
        Path("x.wav"), [(0.0, 1.0, "SPEAKER_00")], vault_root=tmp_path
    )
    assert out is None  # no profiles → anonymous
    assert seen == [tmp_path]


_SAMPLES = Path(__file__).parent / "fixtures" / "media-samples"


@pytest.mark.parametrize(
    "name,library",
    [
        ("sample.epub", "markitdown"),
        ("sample.odt", "odfdo"),
        ("sample.ods", "odfdo"),
        ("sample.odp", "odfdo"),
        ("sample.rtf", "striprtf"),
    ],
)
def test_new_document_formats_extract_their_text(name: str, library: str) -> None:
    pytest.importorskip(library)
    phrase = json.loads((_SAMPLES / "expected.json").read_text(encoding="utf-8"))[name]

    result = extract.extract_text(_SAMPLES / name)

    assert phrase.casefold() in " ".join(result.text.split()).casefold()
    assert result.media_type == extract.media_type_for(name)


def test_ocr_reads_a_script_with_its_own_models_only() -> None:
    from exomem.ocr_models import Model, passes_for

    def model(name: str, script: str) -> Model:
        return Model(name, script, name[:1].isupper(), name.endswith("_vert"))

    installed = (
        model("Latin", "Latin"), model("Japanese", "Han"), model("Japanese_vert", "Han"),
        model("HanS", "Han"), model("HanS_vert", "Han"), model("Hangul", "Hangul"),
        model("eng", "Latin"), model("est", "Latin"), model("jpn", "Han"), model("jpn_vert", "Han"),
    )

    assert passes_for(installed, "Latin") == [("Latin+eng+est", "3")]
    assert passes_for(installed, "Japanese") == [("Japanese+jpn", "3"), ("Japanese_vert+jpn_vert", "5")]
    # Kanji-only text that OSD calls Han is read with the Japanese packs, not Hangul.
    assert passes_for(installed, "Han") == [("HanS+jpn", "3"), ("HanS_vert+jpn_vert", "5")]


def test_a_memory_error_inside_markitdown_is_a_memory_stop(monkeypatch) -> None:
    # MarkItDown wraps a MemoryError under the child's data limit in its own exception.
    # Read as a corrupt file, an oversized document would fail for good instead of
    # counting towards "exceeds this deployment's processing budget".
    pytest.importorskip("markitdown")
    from markitdown.converters import HtmlConverter, PlainTextConverter

    def exhausted(*_args, **_kwargs):
        raise MemoryError

    # Both converters that accept HTML run out, as each does on an oversized page.
    monkeypatch.setattr(HtmlConverter, "convert", exhausted)
    monkeypatch.setattr(PlainTextConverter, "convert", exhausted)

    with pytest.raises(MemoryError):
        extract.extract_text(_SAMPLES / "sample.html")


def test_a_corrupt_pdf_in_a_folder_named_after_an_allocator_is_not_a_memory_stop(tmp_path) -> None:
    # PyMuPDF's errors name the file, so a folder called realloc-notes must not turn a
    # corrupt PDF into a memory stop that marks it over budget.
    pytest.importorskip("fitz")
    folder = tmp_path / "realloc-notes"
    folder.mkdir()
    (folder / "minutes.pdf").write_bytes(b"not a pdf at all")

    with pytest.raises(Exception) as raised:
        extract.extract_text(folder / "minutes.pdf")

    assert not isinstance(raised.value, MemoryError)


def test_a_scanned_pdf_page_releases_its_raster_before_tesseract_starts(tmp_path, monkeypatch) -> None:
    # Tesseract runs under its own data limit beside the media child. A rendered A0 page
    # at 200 dpi is about 186 MB; a child that keeps it during OCR holds two budgets.
    fitz = pytest.importorskip("fitz")
    import gc
    import weakref

    from exomem import ocr_models

    with fitz.open() as doc:
        doc.new_page()  # no text layer, as a scanned page has none
        doc.save(tmp_path / "scan.pdf")
    rendered: list[weakref.ref] = []
    render = fitz.Page.get_pixmap

    def get_pixmap(page, *args, **kwargs):
        pixmap = render(page, *args, **kwargs)
        rendered.append(weakref.ref(pixmap))
        return pixmap

    alive_when_tesseract_starts: list[int] = []

    def read_image(_cmd: str, image: Path) -> str:
        gc.collect()
        alive_when_tesseract_starts.append(sum(ref() is not None for ref in rendered))
        return "harbour lantern was repaired"

    monkeypatch.setattr(fitz.Page, "get_pixmap", get_pixmap)
    monkeypatch.setattr(extract, "resolve_tesseract_cmd", lambda: "tesseract")
    monkeypatch.setattr(ocr_models, "read_image", read_image)

    result = extract.extract_text(tmp_path / "scan.pdf")

    assert result.text == "[page 1]\nharbour lantern was repaired"
    assert alive_when_tesseract_starts == [0]


def test_dark_text_on_a_transparent_image_reaches_tesseract_dark_on_white(tmp_path, monkeypatch) -> None:
    PIL = pytest.importorskip("PIL")
    from PIL import Image, ImageDraw

    from exomem import ocr_models

    source = Image.new("RGBA", (200, 60), (0, 0, 0, 0))
    ImageDraw.Draw(source).rectangle((20, 20, 60, 40), fill=(0, 0, 0, 255))
    source.save(tmp_path / "label.png")
    seen: dict[str, tuple[int, int, int]] = {}

    def read_image(_cmd: str, image: Path) -> str:
        with PIL.Image.open(image) as page:
            rgb = page.convert("RGB")
            seen["background"] = rgb.getpixel((150, 50))
            seen["text"] = rgb.getpixel((40, 30))
        return "label"

    monkeypatch.setattr(extract, "resolve_tesseract_cmd", lambda: "tesseract")
    monkeypatch.setattr(ocr_models, "read_image", read_image)

    extract.extract_text(tmp_path / "label.png")

    assert seen == {"background": (255, 255, 255), "text": (0, 0, 0)}


_FAKE_TESSERACT = '''
import pathlib, sys
args = sys.argv[1:]
if args == ["--list-langs"]:
    print({listing!r})
    sys.exit(0)
if "--psm" in args and args[args.index("--psm") + 1] == "0":
    sys.exit(1)  # OSD is not expected when the inventory is unreadable
base = pathlib.Path(args[1])
tab, newline = chr(9), chr(10)
base.with_suffix(".txt").write_text("harbour lantern was repaired" + newline)
rows = (("level", "conf", "text"), ("5", "91", "harbour"), ("5", "90", "lantern"))
base.with_suffix(".tsv").write_text(newline.join(tab.join(row) for row in rows) + newline)
'''


def _fake_tesseract(tmp_path: Path, listing: str) -> str:
    script = tmp_path / "tesseract"
    script.write_text(f"#!{sys.executable}\n" + _FAKE_TESSERACT.format(listing=listing))
    script.chmod(0o755)
    return str(script)


@pytest.mark.skipif(sys.platform == "win32", reason="the fake Tesseract is a POSIX script")
def test_tesseract_4_without_a_tessdata_path_still_reads_the_image(tmp_path) -> None:
    # Tesseract 4.1 (Ubuntu 22.04) lists its languages without their directory. Taken
    # as "OCR unavailable", every image would block with install wording forever.
    from exomem import ocr_models

    tesseract = _fake_tesseract(tmp_path, "List of available languages (2):\neng\nosd")
    image = tmp_path / "page.png"
    image.write_bytes(b"png")

    assert ocr_models.read_image(tesseract, image) == "harbour lantern was repaired"


def _traineddata(path: Path, script: str) -> None:
    """A minimal traineddata: an LSTM entry and an LSTM unicharset in `script`."""
    import struct

    unicharset = f"3\nNULL 0 Common 0\na 3 {script} 1\nb 3 {script} 2\n".encode()
    header = 4 + 22 * 8
    offsets = [-1] * 22
    offsets[17] = header
    offsets[21] = header + 4
    path.write_bytes(struct.pack("<i", 22) + struct.pack("<22q", *offsets) + b"lstm" + unicharset)


@pytest.mark.skipif(sys.platform == "win32", reason="the fake Tesseract is a POSIX script")
def test_a_fresh_process_reads_the_ocr_inventory_from_its_cache(tmp_path) -> None:
    # Every media child is fresh; re-reading each pack (hundreds of MB) per job is what
    # the inventory cache avoids, while a changed pack set must still be seen.
    import os
    import subprocess

    tessdata = tmp_path / "tessdata"
    tessdata.mkdir()
    _traineddata(tessdata / "eng.traineddata", "Latin")
    env = {**os.environ, "EXOMEM_OCR_INVENTORY": str(tmp_path / "inventory.json")}
    probe = "import sys; from exomem import ocr_models; print([(m.name, m.script) for m in ocr_models.installed_models(sys.argv[1])])"

    def fresh_inventory(listing: str) -> str:
        tesseract = _fake_tesseract(tmp_path, listing)
        result = subprocess.run(
            [sys.executable, "-c", probe, tesseract], env=env, capture_output=True, text=True, check=True
        )
        return result.stdout.strip()

    listing = f'List of available languages in "{tessdata}/" (1):\neng'
    assert fresh_inventory(listing) == "[('eng', 'Latin')]"
    # Same name, size and mtime: a fresh process trusts the cache and reads no pack.
    stat = (tessdata / "eng.traineddata").stat()
    (tessdata / "eng.traineddata").write_bytes(b"\0" * stat.st_size)
    os.utime(tessdata / "eng.traineddata", ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert fresh_inventory(listing) == "[('eng', 'Latin')]"
    # A pack added to the deployment changes the listing, so the inventory is read again.
    _traineddata(tessdata / "est.traineddata", "Latin")
    assert "('est', 'Latin')" in fresh_inventory(f'List of available languages in "{tessdata}/" (2):\neng\nest')
