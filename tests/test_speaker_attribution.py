"""Unit tests for voice-profile speaker attribution (pure, numpy-only)."""
from __future__ import annotations

import numpy as np

from exomem.speaker_attribution import (
    Attribution,
    Profile,
    _group_clusters,
    attribute_clusters,
    cosine,
    count_distinct_speakers,
)


def _unit(*vals: float) -> np.ndarray:
    v = np.array(vals, dtype=float)
    return v / np.linalg.norm(v)


# Two near-orthogonal "voiceprints" — distinct speakers; cosine ~0.
ALICE = _unit(1.0, 0.0, 0.0)
BOB = _unit(0.0, 1.0, 0.0)
STRANGER = _unit(0.0, 0.0, 1.0)


def test_cosine_basic():
    assert cosine([1, 0], [1, 0]) == 1.0
    assert abs(cosine([1, 0], [0, 1])) < 1e-9
    assert cosine([0, 0], [1, 1]) == 0.0  # zero vector → 0, no div error


def test_clear_match_is_named():
    profiles = {"ALICE": Profile("ALICE", ALICE)}
    # cluster c0 is essentially ALICE's voiceprint
    attrs = attribute_clusters({"c0": ALICE}, {"c0": 0.0}, profiles)
    assert attrs["c0"].label == "ALICE"
    assert attrs["c0"].matched_profile == "ALICE"
    assert attrs["c0"].confidence >= 0.99


def test_below_threshold_stays_anonymous():
    # STRANGER is orthogonal to ALICE (cosine ~0) → far below the 0.40 floor.
    profiles = {"ALICE": Profile("ALICE", ALICE)}
    attrs = attribute_clusters({"c0": STRANGER}, {"c0": 0.0}, profiles)
    assert attrs["c0"].label == "Speaker A"
    assert attrs["c0"].matched_profile is None


def test_no_profiles_all_anonymous_by_onset():
    attrs = attribute_clusters(
        {"c0": ALICE, "c1": BOB},
        {"c0": 10.0, "c1": 2.0},  # c1 starts earlier → Speaker A
        {},
    )
    assert attrs["c1"].label == "Speaker A"
    assert attrs["c0"].label == "Speaker B"
    assert all(a.matched_profile is None for a in attrs.values())


def test_two_speakers_named_and_anonymous_mix():
    profiles = {"ALICE": Profile("ALICE", ALICE)}
    attrs = attribute_clusters(
        {"c0": ALICE, "c1": STRANGER},
        {"c0": 0.0, "c1": 5.0},
        profiles,
    )
    assert attrs["c0"].label == "ALICE"
    assert attrs["c1"].label == "Speaker A"  # the lone unmatched group
    assert count_distinct_speakers(attrs) == 2


def test_ambiguous_within_margin_prefers_anonymous():
    # A cluster equally close to ALICE and BOB (the average direction) — within-margin → anonymous.
    blended = _unit(1.0, 1.0, 0.0)  # cosine ~0.707 to both ALICE and BOB
    profiles = {"ALICE": Profile("ALICE", ALICE), "BOB": Profile("BOB", BOB)}
    attrs = attribute_clusters({"c0": blended}, {"c0": 0.0}, profiles)
    # best - second_best ≈ 0 < margin(0.05) → not named.
    assert attrs["c0"].matched_profile is None
    assert attrs["c0"].label == "Speaker A"


def test_over_split_speaker_is_merged_then_named():
    # Two clusters that are the SAME speaker (mutual cosine ~1.0, above 0.50 merge) — should merge
    # into one group, and a single profile labels both.
    ALICE_a = _unit(1.0, 0.02, 0.0)
    ALICE_b = _unit(1.0, 0.0, 0.02)
    groups = _group_clusters(["a", "b"], {"a": ALICE_a, "b": ALICE_b}, 0.50)
    assert len(groups) == 1  # merged

    profiles = {"ALICE": Profile("ALICE", ALICE)}
    attrs = attribute_clusters({"a": ALICE_a, "b": ALICE_b}, {"a": 0.0, "b": 3.0}, profiles)
    assert attrs["a"].label == "ALICE"
    assert attrs["b"].label == "ALICE"
    assert count_distinct_speakers(attrs) == 1


def test_distinct_speakers_not_merged():
    # ALICE and BOB are orthogonal (cosine ~0 < 0.50) → stay separate groups.
    groups = _group_clusters(["a", "b"], {"a": ALICE, "b": BOB}, 0.50)
    assert len(groups) == 2


def test_determinism():
    profiles = {"ALICE": Profile("ALICE", ALICE), "BOB": Profile("BOB", BOB)}
    emb = {"c0": ALICE, "c1": BOB, "c2": STRANGER}
    onset = {"c0": 0.0, "c1": 1.0, "c2": 2.0}
    a1 = attribute_clusters(emb, onset, profiles)
    a2 = attribute_clusters(emb, onset, profiles)
    assert {k: (v.label, v.matched_profile) for k, v in a1.items()} == {
        k: (v.label, v.matched_profile) for k, v in a2.items()
    }


def test_attribution_is_a_frozen_dataclass():
    a = Attribution("ALICE", 0.9, "ALICE")
    assert (a.label, a.confidence, a.matched_profile) == ("ALICE", 0.9, "ALICE")
