"""Which track hears a segment best: a track keeps a segment only where it is the dominant source.

Each microphone also hears the other speakers, so one sentence would appear once in every track.
For each segment the tracks' speech energy over the segment's span is compared, each track's
energy divided by that track's own speech level. The segment stays when its track is the loudest
or within the policy's margin of the loudest, so two people who speak at once keep both segments.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from typing import Protocol

import numpy as np


class Segment(Protocol):
    start: float
    end: float


def envelope(chunks: Iterable[np.ndarray], sample_rate: int, hop_seconds: float) -> np.ndarray:
    """Mean power of the audio in each hop, from chunks of any length; the audio never sits in memory whole."""
    hop = max(1, round(sample_rate * hop_seconds))
    powers: list[np.ndarray] = []
    carry = np.zeros(0, dtype=np.float64)
    for chunk in chunks:
        samples = np.concatenate([carry, chunk.astype(np.float64)]) if carry.size else chunk.astype(np.float64)
        whole = (samples.size // hop) * hop
        if whole:
            powers.append((samples[:whole].reshape(-1, hop) ** 2).mean(axis=1))
        carry = samples[whole:]
    if carry.size:
        powers.append(np.array([(carry**2).mean()]))
    return np.concatenate(powers) if powers else np.zeros(0)


class _Energy:
    """Mean power of one track over any span, from a prefix sum of its envelope."""

    def __init__(self, levels: np.ndarray, hop_seconds: float) -> None:
        self._sums = np.concatenate([[0.0], np.cumsum(levels)])
        self._hop = hop_seconds

    def over(self, start: float, end: float) -> float:
        hops = len(self._sums) - 1
        if hops == 0:
            return 0.0
        first = min(int(start / self._hop), hops - 1)
        last = min(max(first + 1, math.ceil(end / self._hop)), hops)
        return float(self._sums[last] - self._sums[first]) / (last - first)


def keep_dominant(
    segments: Mapping[str, Sequence[Segment]],
    envelopes: Mapping[str, np.ndarray],
    *,
    hop_seconds: float,
    margin_db: float,
) -> dict[str, list[Segment]]:
    """Each track's segments where the track is within `margin_db` of the loudest track, by its own speech level.

    A track's speech level is the median of its own segments' mean power, so bleed that the track also
    transcribed does not lower it. A track whose envelope is missing
    leaves every track's segments whole: dropping a speaker's words on a measurement that failed would lose them.
    """
    if len(segments) < 2 or any(name not in envelopes for name in segments):
        return {name: list(found) for name, found in segments.items()}
    energy = {name: _Energy(envelopes[name], hop_seconds) for name in segments}
    level = {
        name: float(np.median([energy[name].over(s.start, s.end) for s in found])) if found else 0.0
        for name, found in segments.items()
    }
    ratio = 10 ** (-margin_db / 10)
    kept: dict[str, list[Segment]] = {name: [] for name in segments}
    for name, found in segments.items():
        for segment in found:
            heard = {
                other: (energy[other].over(segment.start, segment.end) / level[other]) if level[other] > 0 else 0.0
                for other in segments
            }
            if heard[name] >= max(heard.values()) * ratio:
                kept[name].append(segment)
    return kept
