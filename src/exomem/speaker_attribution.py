"""Speaker attribution: map diarized clusters → enrolled voice profiles.

Each anonymous diarization cluster is matched against the set of enrolled voice profiles. The
best match above a profile's threshold (by a margin over the cluster's second-best profile, and
either confidently high OR clearly standing out from other clusters) wins the named label; each
profile may label more than one cluster-group if a speaker is split across non-merging groups.
Unmatched clusters become stable anonymous `Speaker A/B/…` labels by first-onset order.

Pure / torch-free (numpy only) so it unit-tests without a GPU. Ported from a production
`speaker_attribution` module (single-host host-vs-guest → multi-profile), adapted to
exomem's anonymous `Speaker A` labelling and `EXOMEM_VOICE_*` env overrides.

The attribution is a deterministic *measurement* — a frozen cosine comparison against an
enrolled centroid with fixed thresholds — not a judgment and not an LLM. It prefers leaving a
cluster anonymous over assigning an uncertain name (never mis-names).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Hashable, Mapping, Optional

import numpy as np


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# A group's best profile must beat its second-best by at least this to be named (guards against
# confusable enrolled speakers, e.g. two household members).
DEFAULT_MARGIN = _env_float("EXOMEM_VOICE_MARGIN", 0.05)
# Two clusters whose mutual cosine >= this are the SAME speaker and are merged (undoes diarizer
# over-splitting). ECAPA cross-speaker cosine tops out ~0.4, within-speaker ~0.6+, so 0.5 keeps
# distinct speakers apart while re-merging over-split fragments.
DEFAULT_MERGE_THRESHOLD = _env_float("EXOMEM_VOICE_MERGE_THRESHOLD", 0.50)
# A group is named only if it clears its profile threshold (floor) AND either scores confidently
# (threshold + CONFIDENT_DELTA) OR clearly out-scores every other group for that profile (REL_GAP).
DEFAULT_CONFIDENT_DELTA = _env_float("EXOMEM_VOICE_CONFIDENT_DELTA", 0.15)
DEFAULT_REL_GAP = _env_float("EXOMEM_VOICE_REL_GAP", 0.10)


def _anon_label(i: int) -> str:
    """0-indexed guest order → 'Speaker A', 'Speaker B', … (matches exomem's anonymous scheme)."""
    return f"Speaker {chr(ord('A') + i)}" if i < 26 else f"Speaker {i + 1}"


@dataclass(frozen=True)
class Profile:
    """An enrolled speaker: a name, a voice-embedding centroid, and a match threshold."""

    name: str
    centroid: np.ndarray
    # Evidence-based default (from Q): named speakers scored ~0.44–0.62, others <=0.19, so 0.40
    # cleanly separates them. Per-profile overridable.
    threshold: float = 0.40


@dataclass(frozen=True)
class Attribution:
    """Result for one cluster: the resolved label, a confidence, and the matched profile (or None)."""

    label: str
    confidence: float
    matched_profile: Optional[str]


def cosine(a, b) -> float:
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return float(np.dot(a.ravel(), b.ravel()) / (na * nb))


def _group_clusters(
    cluster_ids: list, embeddings: Mapping[Hashable, np.ndarray], merge_threshold: float
) -> list[list]:
    """Merge clusters that are the same speaker into groups via **average-linkage** (UPGMA).

    Undoes diarizer over-splitting so attribution decides per *speaker*, not per fragment. Groups
    merge only when their *average* pairwise cosine clears ``merge_threshold``. Average-linkage
    (not single-linkage) prevents a weak near-threshold bridge fragment from chaining two genuinely
    distinct speakers into one. Returns a deterministic partition of every input cluster_id.
    """
    n = len(cluster_ids)
    if n == 0:
        return []
    if n == 1:
        return [list(cluster_ids)]

    mat = np.stack([np.asarray(embeddings[c], dtype=float).ravel() for c in cluster_ids])
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    unit = mat / np.where(norms == 0.0, 1.0, norms)
    dist = 1.0 - np.clip(unit @ unit.T, -1.0, 1.0)

    cutoff = 1.0 - merge_threshold
    members: dict[int, list[int]] = {i: [i] for i in range(n)}
    sizes: dict[int, int] = {i: 1 for i in range(n)}
    gdist = dist.astype(float).copy()
    np.fill_diagonal(gdist, np.inf)
    live = list(range(n))  # ascending, so the lower-index slot always survives a merge

    while len(live) > 1:
        best_d, lo, hi = np.inf, -1, -1
        for x in range(len(live)):
            for y in range(x + 1, len(live)):
                a, b = live[x], live[y]
                if gdist[a, b] < best_d:
                    best_d, lo, hi = gdist[a, b], a, b
        if best_d > cutoff:
            break
        na, nb = sizes[lo], sizes[hi]
        for c in live:
            if c == lo or c == hi:
                continue
            gdist[lo, c] = gdist[c, lo] = (na * gdist[lo, c] + nb * gdist[hi, c]) / (na + nb)
        sizes[lo] = na + nb
        members[lo].extend(members[hi])
        live.remove(hi)

    return [
        [cluster_ids[i] for i in sorted(members[slot])]
        for slot in sorted(live, key=lambda s: min(members[s]))
    ]


def attribute_clusters(
    cluster_embeddings: Mapping[Hashable, np.ndarray],
    first_onset: Mapping[Hashable, float],
    profiles: Mapping[str, Profile],
    *,
    margin: float = DEFAULT_MARGIN,
    merge_threshold: float = DEFAULT_MERGE_THRESHOLD,
    confident_delta: float = DEFAULT_CONFIDENT_DELTA,
    rel_gap: float = DEFAULT_REL_GAP,
) -> dict[Hashable, Attribution]:
    """Attribute each diarized cluster to a named profile or a stable anonymous `Speaker #` label.

    Clusters are first grouped into *speakers* (``merge_threshold`` on mutual cosine), then the
    named-vs-anonymous decision is made on each group's MEAN embedding — so one unlucky high-scoring
    fragment can't flip the call. A group is named its best profile only if it clears the floor +
    cross-profile margin AND (scores confidently OR clearly out-scores every other group for that
    profile). Every input cluster maps to its group's resolved label.
    """
    cluster_ids = list(cluster_embeddings)
    profile_list = list(profiles.values())
    groups = _group_clusters(cluster_ids, cluster_embeddings, merge_threshold)

    # Pass 1: each speaker-group's mean embedding, per-profile cosines, and first onset.
    group_info: list[tuple[list, dict[str, float], float]] = []
    for members in groups:
        gcent = np.mean(
            np.stack([np.asarray(cluster_embeddings[c], dtype=float).ravel() for c in members]),
            axis=0,
        )
        scores = {p.name: cosine(gcent, p.centroid) for p in profile_list}
        g_onset = min((first_onset.get(c, float("inf")) for c in members), default=float("inf"))
        group_info.append((members, scores, g_onset))

    # Pass 2: decide each group RELATIVE to the others.
    result: dict[Hashable, Attribution] = {}
    anon_groups: list[tuple[float, list, float]] = []
    for idx, (members, scores, g_onset) in enumerate(group_info):
        named: Optional[tuple[str, float]] = None
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        if ranked:
            best_name, best_score = ranked[0]
            second_score = ranked[1][1] if len(ranked) > 1 else float("-inf")
            threshold = profiles[best_name].threshold
            other_best = max(
                (group_info[j][1][best_name] for j in range(len(group_info)) if j != idx),
                default=float("-inf"),
            )
            clears_floor = best_score >= threshold
            beats_other_profiles = (best_score - second_score) >= margin
            confident = best_score >= threshold + confident_delta
            stands_out = (best_score - other_best) >= rel_gap
            if clears_floor and beats_other_profiles and (confident or stands_out):
                named = (best_name, round(best_score, 4))
        if named is not None:
            for c in members:
                result[c] = Attribution(named[0], named[1], named[0])
        else:
            nearest = round(max(scores.values()), 4) if scores else 0.0
            anon_groups.append((g_onset, members, nearest))

    # Unmatched groups → anonymous Speaker A/B/…, enumerated by each group's first onset.
    anon_groups.sort(key=lambda t: (t[0], str(t[1])))
    for i, (_onset, members, nearest) in enumerate(anon_groups):
        for c in members:
            result[c] = Attribution(label=_anon_label(i), confidence=nearest, matched_profile=None)

    return result


def count_distinct_speakers(attributions: Mapping[Hashable, Attribution]) -> int:
    return len({a.label for a in attributions.values()})
