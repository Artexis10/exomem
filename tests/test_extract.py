"""extract.py — media-type dispatch + soft-fail (engines themselves aren't run here)."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

from exomem import extract, speaker_attribution, voice_embed, voice_profiles


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


def test_extract_textfile_reads_utf8(tmp_path) -> None:
    f = tmp_path / "note.txt"
    f.write_text("plain text marker zylo", encoding="utf-8")
    r = extract._extract_textfile(f)
    assert r.media_type == "text" and r.engine == "text"
    assert "zylo" in r.text


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


def test_extract_document_soft_fails_on_bad_input(tmp_path) -> None:
    # markitdown missing → ExtractionUnavailable; present but file missing → convert raises
    # → still ExtractionUnavailable (wrapped). Either way, never a hard crash.
    with pytest.raises(extract.ExtractionUnavailable):
        extract._extract_document(tmp_path / "does-not-exist.docx", "docx")


# ---------------- optional: ASR speaker diarization (EXOMEM_DIARIZE, default OFF) ----


class _FakeSeg:
    def __init__(self, text: str, start: float, end: float) -> None:
        self.text = text
        self.start = start
        self.end = end


class _FakeWhisper:
    def __init__(self, segs: list) -> None:
        self._segs = segs

    def transcribe(self, path):  # faster-whisper returns (segments_generator, info)
        return iter(self._segs), None


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
    monkeypatch.setattr(voice_profiles, "voice_profiles_path", lambda root: root / "profiles")
    monkeypatch.setattr(voice_profiles, "load_profiles", lambda _path: {"profile": object()})
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
    monkeypatch.setattr(voice_profiles, "load_profiles", lambda p: {"Hugo": object(), "Maria": object()})
    with caplog.at_level(logging.INFO, logger="exomem.extract"):
        extract.log_diarization_readiness(tmp_path)
    rec = _readiness_record(caplog)
    assert rec.levelno == logging.INFO
    msg = rec.getMessage()
    assert "enabled=True" in msg and "sidecar_venv=True" in msg and "hf_token=True" in msg
    assert "profiles=2" in msg and "Hugo" in msg and "Maria" in msg
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
        return root / "Knowledge Base" / ".voice_profiles.json"

    monkeypatch.setattr(voice_profiles, "voice_profiles_path", _spy_path)
    monkeypatch.setattr(voice_profiles, "load_profiles", lambda p: {})
    out = extract._resolve_named_labels(
        Path("x.wav"), [(0.0, 1.0, "SPEAKER_00")], vault_root=tmp_path
    )
    assert out is None  # no profiles → anonymous
    assert seen == [tmp_path]
