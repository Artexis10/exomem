"""The wordbank's names, partitioned by split.

A world draws every entity name from the partition of its own split, so the splits share no name.
The names come from membench's wordbank (``membench.wordbank``), which composes them from syllables,
so no real person, company or place is among them. This module adds the partition and nothing else:
one name is one index of one wordbank space, and a stable hash of that index gives its split.
"""

from __future__ import annotations

import hashlib
import itertools
from collections.abc import Callable, Collection
from functools import cache
from random import Random

from membench import wordbank

# The closed set that `name_spaces` in attributes.yaml declares: each space with its size and the
# wordbank function that names its n-th member.
_SPACES: dict[str, tuple[int, Callable[[int], str]]] = {
    "person": (wordbank.PERSON_SPACE, wordbank.person_name_at),
    "org": (wordbank.ORG_SPACE, wordbank.org_name_at),
    "project": (wordbank.PROJECT_SPACE, wordbank.project_name_at),
    "city": (wordbank.CITY_SPACE, wordbank.city_name_at),
    "product": (wordbank.PRODUCT_SPACE, wordbank.product_name_at),
    "concept": (wordbank.CONCEPT_SPACE, wordbank.concept_name_at),
}

# Random draws that `NamePool.take` makes before it scans the partition for a free name.
_DRAWS = 16


def implemented() -> dict[str, tuple[str, ...]]:
    """The closed values that this module implements, for comparison with ``attributes.yaml``."""
    return {"name_spaces": tuple(sorted(_SPACES))}


def name_at(space: str, index: int) -> str:
    return _SPACES[space][1](index)


def split_of(space: str, index: int, splits: Collection[str]) -> str:
    """The split that one name belongs to. ``splits`` is the declared set, in any order."""
    ordered = sorted(splits)
    digest = hashlib.sha256(f"{space}:{index}".encode()).digest()
    return ordered[int.from_bytes(digest[:8], "big") % len(ordered)]


@cache
def partition(space: str, split: str, splits: tuple[str, ...]) -> tuple[int, ...]:
    """Every index of a name space that belongs to ``split``, in ascending order."""
    return tuple(i for i in range(_SPACES[space][0]) if split_of(space, i, splits) == split)


class Exhausted(Exception):
    """A split holds no more unused names in a space."""


class NamePool:
    """The names of one world: each is drawn once, from the partition of the world's split."""

    def __init__(self, split: str, splits: Collection[str]):
        self.split = split
        self._splits = tuple(sorted(splits))
        self._used: set[str] = set()

    def take(self, space: str, rng: Random) -> str:
        """An unused name of ``space`` from this split's partition, drawn with ``rng``."""
        indices = partition(space, self.split, self._splits)
        # A random draw finds a free name at once unless the partition is nearly used up; the scan
        # then finds the first free one, so the draw is bounded and the outcome stays deterministic.
        draws = (indices[rng.randrange(len(indices))] for _ in range(_DRAWS) if indices)
        for index in itertools.chain(draws, indices):
            name = name_at(space, index)
            if name not in self._used:
                self._used.add(name)
                return name
        raise Exhausted(f"split {self.split} has no unused name in space {space}")
