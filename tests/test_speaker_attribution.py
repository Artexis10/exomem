"""Unit tests for voice-profile speaker attribution (pure, numpy-only)."""
from __future__ import annotations

import numpy as np

from kb_mcp.speaker_attribution import (
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
HUGO = _unit(1.0, 0.0, 0.0)
KIM = _unit(0.0, 1.0, 0.0)
STRANGER = _unit(0.0, 0.0, 1.0)


def test_cosine_basic():
    assert cosine([1, 0], [1, 0]) == 1.0
    assert abs(cosine([1, 0], [0, 1])) < 1e-9
    assert cosine([0, 0], [1, 1]) == 0.0  # zero vector → 0, no div error


def test_clear_match_is_named():
    profiles = {"Hugo": Profile("Hugo", HUGO)}
    # cluster c0 is essentially Hugo's voiceprint
    attrs = attribute_clusters({"c0": HUGO}, {"c0": 0.0}, profiles)
    assert attrs["c0"].label == "Hugo"
    assert attrs["c0"].matched_profile == "Hugo"
    assert attrs["c0"].confidence >= 0.99


def test_below_threshold_stays_anonymous():
    # STRANGER is orthogonal to Hugo (cosine ~0) → far below the 0.40 floor.
    profiles = {"Hugo": Profile("Hugo", HUGO)}
    attrs = attribute_clusters({"c0": STRANGER}, {"c0": 0.0}, profiles)
    assert attrs["c0"].label == "Speaker A"
    assert attrs["c0"].matched_profile is None


def test_no_profiles_all_anonymous_by_onset():
    attrs = attribute_clusters(
        {"c0": HUGO, "c1": KIM},
        {"c0": 10.0, "c1": 2.0},  # c1 starts earlier → Speaker A
        {},
    )
    assert attrs["c1"].label == "Speaker A"
    assert attrs["c0"].label == "Speaker B"
    assert all(a.matched_profile is None for a in attrs.values())


def test_two_speakers_named_and_anonymous_mix():
    profiles = {"Hugo": Profile("Hugo", HUGO)}
    attrs = attribute_clusters(
        {"c0": HUGO, "c1": STRANGER},
        {"c0": 0.0, "c1": 5.0},
        profiles,
    )
    assert attrs["c0"].label == "Hugo"
    assert attrs["c1"].label == "Speaker A"  # the lone unmatched group
    assert count_distinct_speakers(attrs) == 2


def test_ambiguous_within_margin_prefers_anonymous():
    # A cluster equally close to Hugo and Kim (the average direction) — within-margin → anonymous.
    blended = _unit(1.0, 1.0, 0.0)  # cosine ~0.707 to both Hugo and Kim
    profiles = {"Hugo": Profile("Hugo", HUGO), "Kim": Profile("Kim", KIM)}
    attrs = attribute_clusters({"c0": blended}, {"c0": 0.0}, profiles)
    # best - second_best ≈ 0 < margin(0.05) → not named.
    assert attrs["c0"].matched_profile is None
    assert attrs["c0"].label == "Speaker A"


def test_over_split_speaker_is_merged_then_named():
    # Two clusters that are the SAME speaker (mutual cosine ~1.0, above 0.50 merge) — should merge
    # into one group, and a single profile labels both.
    hugo_a = _unit(1.0, 0.02, 0.0)
    hugo_b = _unit(1.0, 0.0, 0.02)
    groups = _group_clusters(["a", "b"], {"a": hugo_a, "b": hugo_b}, 0.50)
    assert len(groups) == 1  # merged

    profiles = {"Hugo": Profile("Hugo", HUGO)}
    attrs = attribute_clusters({"a": hugo_a, "b": hugo_b}, {"a": 0.0, "b": 3.0}, profiles)
    assert attrs["a"].label == "Hugo"
    assert attrs["b"].label == "Hugo"
    assert count_distinct_speakers(attrs) == 1


def test_distinct_speakers_not_merged():
    # Hugo and Kim are orthogonal (cosine ~0 < 0.50) → stay separate groups.
    groups = _group_clusters(["a", "b"], {"a": HUGO, "b": KIM}, 0.50)
    assert len(groups) == 2


def test_determinism():
    profiles = {"Hugo": Profile("Hugo", HUGO), "Kim": Profile("Kim", KIM)}
    emb = {"c0": HUGO, "c1": KIM, "c2": STRANGER}
    onset = {"c0": 0.0, "c1": 1.0, "c2": 2.0}
    a1 = attribute_clusters(emb, onset, profiles)
    a2 = attribute_clusters(emb, onset, profiles)
    assert {k: (v.label, v.matched_profile) for k, v in a1.items()} == {
        k: (v.label, v.matched_profile) for k, v in a2.items()
    }


def test_attribution_is_a_frozen_dataclass():
    a = Attribution("Hugo", 0.9, "Hugo")
    assert (a.label, a.confidence, a.matched_profile) == ("Hugo", 0.9, "Hugo")
