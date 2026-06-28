"""Unit tests for the local voice-profile JSON store (pure, numpy + stdlib)."""
from __future__ import annotations

from pathlib import Path

import numpy as np

from kb_mcp import voice_profiles as vp


def test_store_path_is_operational_infra_not_a_note_tree():
    p = vp.voice_profiles_path(Path("/vault"))
    # Dot-prefixed file in the KB root, beside the embedding sidecars — never under a note tree.
    assert p.name == ".voice_profiles.json"
    assert p.name.startswith(".")
    assert "Knowledge Base" in p.parts
    assert not any(seg in ("Notes", "Entities", "Sources", "Evidence") for seg in p.parts)


def test_missing_and_corrupt_store_reads_empty(tmp_path):
    missing = tmp_path / "nope.json"
    assert vp.load_store(missing) == {}
    assert vp.load_profiles(missing) == {}
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json", encoding="utf-8")
    assert vp.load_store(corrupt) == {}
    assert vp.load_profiles(corrupt) == {}


def test_save_list_remove_round_trip(tmp_path):
    path = tmp_path / ".voice_profiles.json"
    vp.save_profile(path, "Hugo", np.array([1.0, 0.0, 0.0]), is_self=True)
    listed = vp.list_profiles(path)
    assert len(listed) == 1
    assert listed[0]["name"] == "Hugo"
    assert listed[0]["is_self"] is True
    assert listed[0]["samples"] == 1

    profiles = vp.load_profiles(path)
    assert "Hugo" in profiles
    assert profiles["Hugo"].name == "Hugo"
    assert profiles["Hugo"].centroid.shape == (3,)

    assert vp.remove_profile(path, "Hugo") is True
    assert vp.list_profiles(path) == []
    assert vp.remove_profile(path, "Hugo") is False  # already gone


def test_multi_sample_running_average(tmp_path):
    path = tmp_path / ".voice_profiles.json"
    vp.save_profile(path, "Hugo", np.array([0.0, 0.0, 0.0]))
    rec = vp.save_profile(path, "Hugo", np.array([3.0, 6.0, 9.0]))
    # running average of [0,0,0] and [3,6,9] over 2 samples → [1.5, 3, 4.5]
    assert rec["samples"] == 2
    np.testing.assert_allclose(rec["centroid"], [1.5, 3.0, 4.5])
    # is_self sticks once set, even if a later sample omits it
    vp.save_profile(path, "Hugo", np.array([0.0, 0.0, 0.0]), is_self=True)
    rec2 = vp.save_profile(path, "Hugo", np.array([0.0, 0.0, 0.0]))
    assert rec2["is_self"] is True
    assert rec2["samples"] == 4


def test_load_profiles_skips_malformed_entries(tmp_path):
    path = tmp_path / ".voice_profiles.json"
    path.write_text(
        '{"Good": {"centroid": [1,0,0], "threshold": 0.4, "samples": 1},'
        ' "NoCentroid": {"threshold": 0.4}, "Junk": 5}',
        encoding="utf-8",
    )
    profiles = vp.load_profiles(path)
    assert set(profiles) == {"Good"}
