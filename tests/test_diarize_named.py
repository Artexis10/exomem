"""Named-speaker attribution wired into extract._diarize (pyannote + ECAPA patched).

No real models run: `_run_diarization` is stubbed to fixed turns, `voice_embed.embed_spans` to
fixed vectors, and the profile store / vault resolution are patched. Covers the four contract
cases — enrolled→named, unknown→anonymous, no-profiles→byte-identical, embed-soft-fail→anonymous
— plus that the resolved name reaches the structured `speakers` field that flows to the sidecar.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from kb_mcp import extract, vault, voice_embed, voice_profiles
from kb_mcp.speaker_attribution import Profile


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


# Orthogonal unit "voiceprints": the enrolled owner vs. two distinct unknown voices (cosine ~0,
# so the two strangers don't merge into one anonymous cluster either).
_OWNER = np.array([1.0, 0.0, 0.0])
_STRANGER = np.array([0.0, 1.0, 0.0])
_STRANGER2 = np.array([0.0, 0.0, 1.0])

# Two segments diarized into two distinct raw clusters, one per second.
_SEGS = [_FakeSeg("hello there", 0.0, 1.0), _FakeSeg("general kenobi", 1.0, 2.0)]
_TURNS = [(0.0, 1.0, "SPEAKER_00"), (1.0, 2.0, "SPEAKER_01")]


def _wire(monkeypatch, *, profiles, embed):
    """Enable diarization and patch the whisper, pyannote, profile-store, and embedder seams."""
    monkeypatch.setenv("KB_MCP_DIARIZE", "1")
    monkeypatch.setattr(extract, "_get_whisper", lambda: _FakeWhisper(list(_SEGS)))
    monkeypatch.setattr(extract, "_run_diarization", lambda p: list(_TURNS))
    monkeypatch.setattr(vault, "resolve_vault", lambda: Path("/vault"))
    monkeypatch.setattr(voice_profiles, "load_profiles", lambda path: dict(profiles))
    monkeypatch.setattr(voice_embed, "embed_spans", embed)


def test_enrolled_voice_is_named(monkeypatch: pytest.MonkeyPatch) -> None:
    # SPEAKER_00 (starts at 0.0) is the owner; SPEAKER_01 is unknown.
    def embed(_path, spans):
        return _OWNER if spans[0][0] < 1.0 else _STRANGER

    _wire(monkeypatch, profiles={"Alex": Profile("Alex", _OWNER)}, embed=embed)
    r = extract._transcribe(Path("x.wav"), "audio")

    assert "[Alex]: hello there" in r.text
    assert "[Speaker A]: general kenobi" in r.text  # the unknown cluster stays anonymous
    assert r.engine.endswith("+diarized")
    # The resolved name reaches the structured field that media_worker → preserve persist.
    assert [t["speaker"] for t in r.speakers] == ["Alex", "Speaker A"]


def test_unknown_voice_stays_anonymous(monkeypatch: pytest.MonkeyPatch) -> None:
    # Both clusters embed far from the only profile (and from each other) → two anonymous voices.
    def embed(_path, spans):
        return _STRANGER if spans[0][0] < 1.0 else _STRANGER2

    _wire(monkeypatch, profiles={"Alex": Profile("Alex", _OWNER)}, embed=embed)
    r = extract._transcribe(Path("x.wav"), "audio")

    assert "Alex" not in r.text
    assert "[Speaker A]: hello there" in r.text
    assert "[Speaker B]: general kenobi" in r.text
    assert all(t["speaker"].startswith("Speaker ") for t in r.speakers)


def test_no_profiles_is_byte_identical_to_anonymous(monkeypatch: pytest.MonkeyPatch) -> None:
    # Zero profiles → the named layer returns None and never loads the embedder.
    def _must_not_embed(_p, _s):
        raise AssertionError("embed_spans must not be called when no profiles are enrolled")

    _wire(monkeypatch, profiles={}, embed=_must_not_embed)
    r = extract._transcribe(Path("x.wav"), "audio")

    assert r.text == "[Speaker A]: hello there\n[Speaker B]: general kenobi"
    assert [t["speaker"] for t in r.speakers] == ["Speaker A", "Speaker B"]


def test_embed_soft_fail_falls_back_to_anonymous(monkeypatch: pytest.MonkeyPatch) -> None:
    # Embedder unavailable / errors → None → wholly anonymous (never partially named).
    _wire(monkeypatch, profiles={"Alex": Profile("Alex", _OWNER)}, embed=lambda _p, _s: None)
    r = extract._transcribe(Path("x.wav"), "audio")

    assert r.text == "[Speaker A]: hello there\n[Speaker B]: general kenobi"
    assert [t["speaker"] for t in r.speakers] == ["Speaker A", "Speaker B"]


def test_resolution_failure_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    # resolve_vault blowing up (e.g. KB_MCP_VAULT_PATH unset) must degrade, not crash.
    def _boom():
        raise RuntimeError("KB_MCP_VAULT_PATH is not set")

    monkeypatch.setenv("KB_MCP_DIARIZE", "1")
    monkeypatch.setattr(extract, "_get_whisper", lambda: _FakeWhisper(list(_SEGS)))
    monkeypatch.setattr(extract, "_run_diarization", lambda p: list(_TURNS))
    monkeypatch.setattr(vault, "resolve_vault", _boom)
    r = extract._transcribe(Path("x.wav"), "audio")

    assert r.text == "[Speaker A]: hello there\n[Speaker B]: general kenobi"
