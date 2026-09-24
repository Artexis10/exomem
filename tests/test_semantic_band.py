"""The corpus-relative semantic band (step 4, T3; design §5.1).

A row joins the band when its similarity to the turn clears the level that the
largest of N unrelated similarities exceeds with probability alpha, measured
from the population's own median and MAD. No cosine threshold belongs to one
model, one language or one vault, so none appears here.
"""

from __future__ import annotations

import numpy as np
import pytest

from exomem import ranking_config, working_set_resolve

ALPHA = 0.01
MIN_POPULATION = 50


def _unit(rows: np.ndarray) -> np.ndarray:
    return rows / np.linalg.norm(rows, axis=-1, keepdims=True)


def _band(similarities) -> frozenset[int] | None:
    return working_set_resolve.semantic_band(similarities, alpha=ALPHA, min_population=MIN_POPULATION)


def test_the_policy_is_alpha_and_a_minimum_population_not_a_cosine() -> None:
    config = ranking_config.DEFAULT_RANKING

    assert (config.working_set_semantic_alpha, config.working_set_semantic_min_population) == (0.01, 50)
    assert not hasattr(config, "working_set_vector_strong")
    assert not hasattr(config, "working_set_vector_weak")


def test_a_planted_outlier_among_2000_random_vectors_is_banded() -> None:
    rng = np.random.default_rng(11)
    population = _unit(rng.standard_normal((2000, 64)))
    query = population[1234] + 0.05 * rng.standard_normal(64)

    band = _band(population @ _unit(query))

    assert band == frozenset({1234})


def test_unrelated_turns_band_at_about_the_tolerated_rate() -> None:
    rng = np.random.default_rng(12)
    trials, banded = 1000, 0
    for _ in range(trials):
        population = _unit(rng.standard_normal((200, 64)))
        band = _band(population @ _unit(rng.standard_normal(64)))
        banded += bool(band)

    assert banded / trials <= 2.5 * ALPHA


@pytest.mark.parametrize("transform", ["shift", "scale"])
def test_the_band_ignores_a_constant_offset_and_a_scale_about_the_median(transform: str) -> None:
    """A German turn against an English vault scores lower everywhere, and e5-style
    models compress every cosine: neither may change which rows band."""
    rng = np.random.default_rng(13)
    population = _unit(rng.standard_normal((300, 64)))
    similarities = population @ _unit(population[7] + 0.1 * rng.standard_normal(64))
    median = float(np.median(similarities))
    moved = similarities + 0.2 if transform == "shift" else median + 0.5 * (similarities - median)

    assert _band(similarities) == _band(moved) == frozenset({7})


def test_a_population_below_the_minimum_is_uncalibrated() -> None:
    rng = np.random.default_rng(14)
    similarities = np.concatenate([[0.99], rng.normal(0.1, 0.05, 48)])

    assert _band(similarities) is None
    assert _band(np.concatenate([similarities, [0.1]])) is not None


def test_a_degenerate_population_is_uncalibrated_never_a_division_by_zero() -> None:
    assert _band(np.full(80, 0.3)) is None
    assert _band(np.concatenate([np.full(79, 0.3), [0.9]])) is None, "MAD 0 has no chance level"


def _population(rng: np.random.Generator, outliers: int, n: int = 120) -> tuple[dict[str, np.ndarray], np.ndarray]:
    query = _unit(rng.standard_normal(64))
    vectors = {f"a{i}": _unit(rng.standard_normal(64)) for i in range(n)}
    for i in range(outliers):
        vectors[f"a{i}"] = _unit(query + 0.1 * rng.standard_normal(64))
    return vectors, query


def test_a_turn_similar_to_more_than_three_anchors_bands_none() -> None:
    """A turn close to many anchors is about a topic, the judgement that stops a
    word naming four anchors from being rare."""
    rng = np.random.default_rng(15)
    config = ranking_config.DEFAULT_RANKING

    three, state = working_set_resolve.vector_bands(*_population(rng, 3), config)
    five, five_state = working_set_resolve.vector_bands(*_population(rng, 5), config)

    assert state == five_state == "ready"
    assert {anchor for anchor, banded in three.items() if banded} == {"a0", "a1", "a2"}
    assert not any(five.values())


def test_vector_bands_reports_an_uncalibrated_population() -> None:
    rng = np.random.default_rng(16)
    vectors, query = _population(rng, 1, n=20)

    bands, state = working_set_resolve.vector_bands(vectors, query, ranking_config.DEFAULT_RANKING)

    assert (bands, state) == ({}, "uncalibrated")
