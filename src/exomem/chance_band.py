"""The chance level a similarity must clear, measured from the population it is one of.

Shared by the working set's anchor bands and the CLIP lane, so neither fixes a cosine.
"""

from __future__ import annotations

import math
import statistics
from typing import Any

#: A spread below this is a population whose rows are all alike (empty or
#: identical), which has no chance level to measure a query against.
_BAND_MIN_SPREAD = 1e-6


def semantic_band(similarities: Any, *, alpha: float, min_population: int) -> frozenset[int] | None:
    """Rows whose similarity to the query clears the chance maximum; None when
    the population cannot be calibrated.

    `m = median(s)`, `σ = 1.4826 · MAD(s)`, and `k(N, α) = Φ⁻¹((1-α)^(1/N))` is
    the level the largest of N unrelated similarities exceeds with probability
    α. Row i clears when `s_i ≥ m + k·σ`. The median and MAD are unmoved by the
    few rows a query is really about, and the rule is invariant to adding a
    constant to every similarity or scaling them all, so no number in it
    belongs to one model, one language or one vault. Fewer than
    `min_population` rows, or a degenerate spread, has no chance level (design
    §5.1).
    """
    import numpy as np

    values = np.asarray(similarities, dtype="float64").reshape(-1)
    n = int(values.size)
    if n < max(1, int(min_population)) or not np.all(np.isfinite(values)):
        return None
    centre = float(np.median(values))
    spread = 1.4826 * float(np.median(np.abs(values - centre)))
    if spread < _BAND_MIN_SPREAD:
        return None
    level = statistics.NormalDist().inv_cdf(math.pow(1.0 - float(alpha), 1.0 / n))
    return frozenset(int(i) for i in np.flatnonzero(values >= centre + level * spread))
