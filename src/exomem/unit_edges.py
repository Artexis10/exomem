"""The one write seam for the typed edges that a unit write gives a unit.

In file mode a unit's edges are its `relations` metadata row, so each function returns the row's
new value and the caller renders it inside its governed write. Keep every edge a unit operation
adds flowing through `add_unit_edges`: a stored-edge mode writes its rows here instead, inside the
same governed write, and no caller changes.
"""

from __future__ import annotations

from collections.abc import Iterable

from . import relation_registry

Edge = tuple[str, str]


def add_unit_edges(current: Iterable[Edge], added: Iterable[Edge]) -> tuple[Edge, ...]:
    """The unit's edges after `added`: the current ones first, each distinct edge once, in order."""
    return tuple(dict.fromkeys((*current, *added)))


def supersession_edge(registry: relation_registry.RelationRegistry, anchor: str) -> Edge:
    """The edge by which a successor replaces the unit `anchor` on the same page.

    The relation is the one shipped relation with the `replaces` behaviour, read from the registry
    rather than named here, so the key lives only in the pack. The shipped pack has exactly one;
    any other count is a defect in the pack.
    """
    keys = sorted(registry.keys_with("replaces") & set(registry.core))
    if len(keys) != 1:
        raise ValueError(f"the relation pack must ship one replacing relation, found {keys}")
    return keys[0], f"[[#{anchor}]]"
