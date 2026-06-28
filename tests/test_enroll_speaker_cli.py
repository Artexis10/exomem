"""CLI speaker enrollment (embedder patched) — enroll writes a profile, --self, list/remove.

The ECAPA embedder is stubbed so no model loads; we drive the public functions and the
`python -m kb_mcp` subcommand dispatch against a tmp profile store.
"""
from __future__ import annotations

import numpy as np
import pytest

from kb_mcp import __main__ as cli
from kb_mcp import enroll_speaker, voice_embed, voice_profiles


@pytest.fixture
def sample(tmp_path):
    p = tmp_path / "voice.wav"
    p.write_bytes(b"RIFF....")  # contents are never read — embed_spans is patched
    return p


def test_enroll_writes_a_profile(tmp_path, sample, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(voice_embed, "embed_spans", lambda p, spans: np.array([1.0, 0.0, 0.0]))
    rec = enroll_speaker.enroll_speaker(sample, "Alice", vault_root=tmp_path)

    assert rec["samples"] == 1
    assert rec["is_self"] is False
    store = voice_profiles.voice_profiles_path(tmp_path)
    assert "Alice" in voice_profiles.load_profiles(store)


def test_enroll_self_flag_and_running_average(tmp_path, sample, monkeypatch) -> None:
    monkeypatch.setattr(voice_embed, "embed_spans", lambda p, spans: np.array([0.0, 0.0, 0.0]))
    enroll_speaker.enroll_speaker(sample, "Alice", is_self=True, vault_root=tmp_path)
    monkeypatch.setattr(voice_embed, "embed_spans", lambda p, spans: np.array([2.0, 4.0, 6.0]))
    rec = enroll_speaker.enroll_speaker(sample, "Alice", vault_root=tmp_path)

    assert rec["samples"] == 2
    assert rec["is_self"] is True  # sticks once set
    np.testing.assert_allclose(rec["centroid"], [1.0, 2.0, 3.0])  # running average


def test_enroll_soft_fail_raises(tmp_path, sample, monkeypatch) -> None:
    monkeypatch.setattr(voice_embed, "embed_spans", lambda p, spans: None)
    with pytest.raises(enroll_speaker.EnrollmentError):
        enroll_speaker.enroll_speaker(sample, "Alice", vault_root=tmp_path)


def test_enroll_missing_sample_raises(tmp_path) -> None:
    with pytest.raises(enroll_speaker.EnrollmentError):
        enroll_speaker.enroll_speaker(tmp_path / "nope.wav", "Alice", vault_root=tmp_path)


def test_list_and_remove_round_trip(tmp_path, sample, monkeypatch) -> None:
    monkeypatch.setattr(voice_embed, "embed_spans", lambda p, spans: np.array([1.0, 0.0, 0.0]))
    enroll_speaker.enroll_speaker(sample, "Alice", is_self=True, vault_root=tmp_path)

    listed = enroll_speaker.list_speakers(tmp_path)
    assert [r["name"] for r in listed] == ["Alice"]
    assert listed[0]["is_self"] is True

    assert enroll_speaker.remove_speaker("Alice", tmp_path) is True
    assert enroll_speaker.list_speakers(tmp_path) == []
    assert enroll_speaker.remove_speaker("Alice", tmp_path) is False


def test_cli_dispatch_enroll_list_remove(tmp_path, sample, monkeypatch) -> None:
    monkeypatch.setattr(voice_embed, "embed_spans", lambda p, spans: np.array([1.0, 0.0, 0.0]))
    v = str(tmp_path)

    assert cli.main(["enroll-speaker", "--name", "Alice", "--self", "--vault", v, str(sample)]) == 0
    assert cli.main(["list-speakers", "--vault", v]) == 0
    assert cli.main(["remove-speaker", "--name", "Alice", "--vault", v]) == 0
    # Removing again reports "no profile" but still exits 0 (idempotent CLI).
    assert cli.main(["remove-speaker", "--name", "Alice", "--vault", v]) == 0


def test_cli_enroll_soft_fail_returns_nonzero(tmp_path, sample, monkeypatch) -> None:
    monkeypatch.setattr(voice_embed, "embed_spans", lambda p, spans: None)
    rc = cli.main(["enroll-speaker", "--name", "Alice", "--vault", str(tmp_path), str(sample)])
    assert rc == 1
